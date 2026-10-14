"""Durable, metadata-only request accounting with explicit missing measurements."""

from __future__ import annotations

import datetime as dt
import fcntl
import math
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any


_STATUSES = (
    "in_progress", "success", "failed", "cancelled", "partial", "interrupted"
)
_FIELDS = {
    "id", "started_at", "client_id", "backend", "requested_model", "actual_model",
    "stream", "input_tokens", "output_tokens", "total_tokens", "token_source",
    "request_bytes", "response_bytes", "duration_ms", "ttft_ms", "generation_tps",
    "status", "error_category",
}
_TEXT_FIELDS = {
    "client_id", "backend", "requested_model", "actual_model", "error_category"
}
_COUNT_FIELDS = {"input_tokens", "output_tokens", "total_tokens"}
_BYTE_FIELDS = {"request_bytes", "response_bytes"}
_MEASUREMENT_FIELDS = {"duration_ms", "ttft_ms", "generation_tps"}
_GROUPS = {
    "client_id": "client_id",
    "model": "actual_model",
    "backend": "backend",
    "date": "substr(started_at, 1, 10)",
}
_FILTER_FIELDS = {"client_id", "requested_model", "actual_model", "backend"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY NOT NULL,
    started_at TEXT NOT NULL,
    client_id TEXT,
    backend TEXT,
    requested_model TEXT,
    actual_model TEXT,
    stream INTEGER NOT NULL CHECK (stream IN (0, 1)),
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    total_tokens INTEGER CHECK (total_tokens >= 0),
    token_source TEXT NOT NULL CHECK (token_source IN ('unavailable', 'measured', 'partial')),
    request_bytes INTEGER CHECK (request_bytes >= 0),
    response_bytes INTEGER CHECK (response_bytes >= 0),
    duration_ms REAL CHECK (duration_ms >= 0),
    ttft_ms REAL CHECK (ttft_ms >= 0),
    generation_tps REAL CHECK (generation_tps >= 0),
    status TEXT NOT NULL CHECK (status IN
        ('in_progress', 'success', 'failed', 'cancelled', 'partial', 'interrupted')),
    error_category TEXT
);
CREATE INDEX IF NOT EXISTS requests_started ON requests(started_at);
CREATE INDEX IF NOT EXISTS requests_client_started ON requests(client_id, started_at);
"""


class Store:
    """One gateway owner per database, with short synchronous atomic writes.

    Only the listed metadata columns can be written. Measurements are nullable;
    no missing token, byte, or performance measurement is converted into a zero.
    Bytes are application body observations, not network-wire traffic. Recovery
    retains the last persisted input-byte observation (possibly a lower bound)
    and marks final output bytes unknown because writes may have followed it.
    Methods are safe to call from different threads in the owning process.
    """

    def __init__(self, path: str | Path):
        self.path = str(path) if str(path) == ":memory:" else str(Path(path).expanduser().resolve())
        self._mutex = threading.RLock()
        self._lock_fd: int | None = None
        self._connection: sqlite3.Connection | None = None
        try:
            if self.path != ":memory:":
                database = Path(self.path)
                database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._lock_fd = os.open(str(database) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("The statistics database is already owned by another gateway") from None
                # Set private permissions at creation, including on SQLite's WAL files.
                try:
                    descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError("Unsupported statistics database schema version")
            # Version 2 preserves unknown response byte counts after a crash.
            # Rebuild the two formerly NOT NULL columns atomically for existing
            # v1 databases; all measured metadata and request identifiers remain.
            self._connection.execute("BEGIN IMMEDIATE")
            with self._connection:
                if version == 1:
                    create_table = _SCHEMA.split(";", 1)[0].replace(
                        "CREATE TABLE IF NOT EXISTS requests", "CREATE TABLE requests_v2"
                    )
                    self._connection.execute(create_table)
                    columns = ", ".join(sorted(_FIELDS))
                    self._connection.execute(
                        f"INSERT INTO requests_v2 ({columns}) SELECT {columns} FROM requests"
                    )
                    self._connection.execute("DROP TABLE requests")
                    self._connection.execute("ALTER TABLE requests_v2 RENAME TO requests")
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        self._connection.execute(statement)
                self._connection.execute("PRAGMA user_version = 2")
                self._connection.execute(
                    "UPDATE requests SET status = 'interrupted', error_category = 'gateway_restart', "
                    "response_bytes = NULL "
                    "WHERE status = 'in_progress'"
                )
        except BaseException:
            self.close()
            raise

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Statistics store is closed")
        return self._connection

    @staticmethod
    def _validated(record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record, dict) or record.keys() - _FIELDS:
            raise ValueError("Unsupported statistics field")
        result = dict(record)
        for name, value in result.items():
            if name == "id":
                if not isinstance(value, str) or not value or len(value) > 128:
                    raise ValueError("Invalid request identifier")
            elif name == "started_at":
                try:
                    timestamp = dt.datetime.fromisoformat(value)
                except (ValueError, TypeError):
                    raise ValueError("started_at must be an ISO UTC timestamp") from None
                if timestamp.utcoffset() != dt.timedelta(0):
                    raise ValueError("started_at must be an ISO UTC timestamp")
                result[name] = timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")
            elif name in _TEXT_FIELDS:
                if value is not None and (not isinstance(value, str) or len(value) > 512):
                    raise ValueError("Invalid statistics metadata")
            elif name in _COUNT_FIELDS | _BYTE_FIELDS:
                if value is None:
                    continue
                if type(value) is not int or not 0 <= value <= 2**63 - 1:
                    raise ValueError("Counts and byte measurements must be nonnegative integers")
            elif name in _MEASUREMENT_FIELDS:
                if value is not None and (
                    type(value) not in (int, float) or not math.isfinite(value) or value < 0
                ):
                    raise ValueError("Performance measurements must be finite and nonnegative")
            elif name == "stream":
                if type(value) is not bool:
                    raise ValueError("stream must be a boolean")
                result[name] = int(value)
            elif name == "status" and value not in _STATUSES:
                raise ValueError("Invalid request status")
            elif name == "token_source" and value not in ("unavailable", "measured", "partial"):
                raise ValueError("Invalid token source")
        return result

    def begin(self, record: dict[str, Any]) -> None:
        """Persist a request before inference starts; no request content is accepted."""
        values = self._validated(record)
        if not {"id", "started_at"} <= values.keys():
            raise ValueError("Request id and started_at are required")
        values = {
            "stream": 0, "token_source": "unavailable", "request_bytes": 0,
            "response_bytes": 0, "status": "in_progress", **values,
        }
        if values["status"] != "in_progress":
            raise ValueError("New requests must have in_progress status")
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self._mutex:
            connection = self._db()
            with connection:
                connection.execute(
                    f"INSERT INTO requests ({columns}) VALUES ({placeholders})", tuple(values.values())
                )

    def finish(self, request_id: str, updates: dict[str, Any]) -> None:
        """Atomically update an existing request with final observed metadata."""
        values = self._validated(updates)
        if "id" in values or "started_at" in values:
            raise ValueError("Request identity and start time are immutable")
        if not values:
            return
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._mutex:
            connection = self._db()
            with connection:
                cursor = connection.execute(
                    f"UPDATE requests SET {assignments} WHERE id = ?", (*values.values(), request_id)
                )
                if cursor.rowcount != 1:
                    raise KeyError("Request does not exist")

    @staticmethod
    def _where(filters: dict[str, Any] | None) -> tuple[str, list[str]]:
        filters = {} if filters is None else filters
        if not isinstance(filters, dict) or filters.keys() - (_FILTER_FIELDS | {"since", "until"}):
            raise ValueError("Unsupported statistics filter")
        clauses, parameters = [], []
        dates: dict[str, str] = {}
        for name, value in filters.items():
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError("Statistics filters must be nonempty strings")
            if name in ("since", "until"):
                try:
                    date = dt.date.fromisoformat(value)
                    if date.isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise ValueError("Date filters must be YYYY-MM-DD UTC dates") from None
                dates[name] = value
                comparison = ">=" if name == "since" else "<="
                clauses.append(f"substr(started_at, 1, 10) {comparison} ?")
            else:
                clauses.append(f"{name} = ?")
            parameters.append(value)
        if "since" in dates and "until" in dates and dates["since"] > dates["until"]:
            raise ValueError("since must not be later than until")
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters

    def recent(self, filters: dict[str, Any] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        where, parameters = self._where(filters)
        with self._mutex:
            rows = self._db().execute(
                "SELECT * FROM requests" + where + " ORDER BY started_at DESC, id DESC LIMIT ?",
                (*parameters, limit),
            ).fetchall()
        return [{**dict(row), "stream": bool(row["stream"])} for row in rows]

    def aggregate(
        self, filters: dict[str, Any] | None = None, group_by: list[str] | None = None
    ) -> list[dict[str, Any]]:
        groups = [] if group_by is None else group_by
        if (
            not isinstance(groups, list) or any(not isinstance(group, str) or group not in _GROUPS for group in groups)
            or len(set(groups)) != len(groups)
        ):
            raise ValueError("group_by supports client_id, model, backend, and date once each")
        where, parameters = self._where(filters)
        selections = [f"{_GROUPS[group]} AS {group}" for group in groups]
        selections.append("COUNT(*) AS request_count")
        for token in ("input_tokens", "output_tokens", "total_tokens"):
            selections.extend((f"SUM({token}) AS {token}", f"COUNT({token}) AS {token[:-1]}_samples"))
        selections.extend(f"COALESCE(SUM(status = '{status}'), 0) AS {status}_count" for status in _STATUSES)
        selections.extend((
            "SUM(request_bytes) AS request_bytes",
            "SUM(response_bytes) AS response_bytes",
            "COUNT(request_bytes) AS request_byte_samples",
            "COUNT(response_bytes) AS response_byte_samples",
            "AVG(duration_ms) AS avg_duration_ms",
            "COUNT(duration_ms) AS duration_samples",
        ))
        query = "SELECT " + ", ".join(selections) + " FROM requests" + where
        if groups:
            query += " GROUP BY " + ", ".join(_GROUPS[group] for group in groups)
            query += " ORDER BY " + ", ".join(groups)
        with self._mutex:
            return [dict(row) for row in self._db().execute(query, parameters).fetchall()]

    def backup(self, path: str | Path) -> None:
        """Create a consistent SQLite backup, refusing to replace any existing file."""
        target = Path(path).expanduser()
        with self._mutex:
            source = self._db()
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
            try:
                destination = sqlite3.connect(target)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            except BaseException:
                target.unlink(missing_ok=True)
                raise

    def close(self) -> None:
        with self._mutex:
            try:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
            finally:
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
