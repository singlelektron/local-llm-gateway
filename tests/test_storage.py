"""Storage behavior, including real SQLite persistence and missing observations."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from local_llm_gateway.storage import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "usage.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def begin(self, request_id="one", **extra):
        self.store.begin({
            "id": request_id, "started_at": "2026-10-14T12:30:00+00:00",
            "client_id": "independent-example", "backend": "ollama",
            "requested_model": "small", "actual_model": "local-model:latest",
            "stream": False, "request_bytes": 73, **extra,
        })

    def test_immediate_persistence_and_restart(self):
        self.begin()
        with sqlite3.connect(self.path) as reader:
            self.assertEqual(reader.execute("SELECT status FROM requests").fetchone()[0], "in_progress")
        self.store.finish("one", {
            "status": "success", "input_tokens": 4, "output_tokens": 3,
            "total_tokens": 7, "token_source": "measured", "response_bytes": 101,
            "duration_ms": 98.5,
        })
        self.store.close()
        with Store(self.path) as reopened:
            row = reopened.recent()[0]
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["total_tokens"], 7)
            self.assertEqual(row["duration_ms"], 98.5)
            self.assertFalse(row["stream"])
            self.assertEqual(row["started_at"], "2026-10-14T12:30:00.000000Z")

    def test_null_usage_is_not_zero_and_coverage_is_explicit(self):
        self.begin()
        self.store.finish("one", {"status": "cancelled"})
        empty = self.store.aggregate()[0]
        self.assertIsNone(empty["input_tokens"])
        self.assertIsNone(empty["output_tokens"])
        self.assertIsNone(empty["total_tokens"])
        self.assertIsNone(empty["avg_duration_ms"])
        self.assertEqual(empty["total_token_samples"], 0)
        self.assertEqual(empty["cancelled_count"], 1)
        self.begin("two")
        self.store.finish("two", {
            "status": "partial", "input_tokens": 0, "output_tokens": 2,
            "total_tokens": 2, "token_source": "partial", "duration_ms": 40,
        })
        result = self.store.aggregate()[0]
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["input_tokens"], 0)
        self.assertEqual(result["output_tokens"], 2)
        self.assertEqual(result["total_tokens"], 2)
        self.assertEqual(result["total_token_samples"], 1)
        self.assertEqual(result["partial_count"], 1)
        self.assertEqual(result["request_bytes"], 146)
        self.assertEqual(result["response_bytes"], 0)
        self.assertEqual(result["avg_duration_ms"], 40)
        self.assertEqual(result["duration_samples"], 1)

    def test_no_matches_have_no_invented_token_samples(self):
        aggregate = self.store.aggregate({"client_id": "absent"})[0]
        self.assertEqual(aggregate["request_count"], 0)
        self.assertEqual(aggregate["total_token_samples"], 0)
        self.assertIsNone(aggregate["total_tokens"])
        self.assertEqual(aggregate["success_count"], 0)
        self.assertEqual(self.store.aggregate({}, ["client_id"]), [])

    def test_exclusive_ownership_does_not_recover_live_requests(self):
        self.begin()
        with self.assertRaisesRegex(RuntimeError, "already owned"):
            Store(self.path)
        self.assertEqual(self.store.recent()[0]["status"], "in_progress")
        self.store.close()
        with Store(self.path) as reopened:
            row = reopened.recent()[0]
            self.assertEqual(row["status"], "interrupted")
            self.assertEqual(row["error_category"], "gateway_restart")
            self.assertIsNone(row["duration_ms"])
            self.assertIsNone(row["total_tokens"])
            self.assertEqual(row["request_bytes"], 73)
            self.assertIsNone(row["response_bytes"])
            summary = reopened.aggregate()[0]
            self.assertEqual(summary["request_bytes"], 73)
            self.assertEqual(summary["request_byte_samples"], 1)
            self.assertIsNone(summary["response_bytes"])
            self.assertEqual(summary["response_byte_samples"], 0)

    def test_database_alias_does_not_bypass_ownership_lock(self):
        alias = Path(self.directory.name) / "alias.sqlite3"
        alias.symlink_to(self.path)
        with self.assertRaisesRegex(RuntimeError, "already owned"):
            Store(alias)

    def test_version_one_database_is_migrated_without_losing_metadata(self):
        # Recreate the actual older schema constraints without a Store owning it.
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'requests'"
            ).fetchone()[0]
            schema = schema.replace("request_bytes INTEGER", "request_bytes INTEGER NOT NULL")
            schema = schema.replace("response_bytes INTEGER", "response_bytes INTEGER NOT NULL")
            connection.execute("DROP TABLE requests")
            connection.execute(schema)
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                "INSERT INTO requests (id, started_at, stream, token_source, "
                "request_bytes, response_bytes, status, total_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("old", "2026-10-14T00:00:00.000000Z", 0, "measured", 91, 67, "success", 12),
            )
        with Store(self.path) as migrated:
            row = migrated.recent()[0]
            self.assertEqual(row["id"], "old")
            self.assertEqual(row["total_tokens"], 12)
            self.assertEqual(row["response_bytes"], 67)
            migrated.finish("old", {"response_bytes": None})
            self.assertIsNone(migrated.recent()[0]["response_bytes"])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_real_process_crash_recovery(self):
        crash_path = Path(self.directory.name) / "crash.sqlite3"
        process = subprocess.run(
            [sys.executable, "-c", (
                "import os, sys\n"
                "from local_llm_gateway.storage import Store\n"
                "store = Store(sys.argv[1])\n"
                "store.begin({'id': 'crashed', 'started_at': '2026-10-14T23:59:59Z', 'stream': True})\n"
                "os._exit(17)\n"
            ), str(crash_path)],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(process.returncode, 17, process.stderr)
        with Store(crash_path) as reopened:
            row = reopened.recent()[0]
            self.assertEqual(row["status"], "interrupted")
            self.assertEqual(row["error_category"], "gateway_restart")
            self.assertTrue(row["stream"])
            self.assertIsNone(row["ttft_ms"])

    def test_filtering_and_grouping_inclusive_utc_dates(self):
        self.begin("before", started_at="2026-10-13T23:59:59.999999Z")
        self.begin("first", started_at="2026-10-14T00:00:00Z")
        self.begin("last", started_at="2026-10-14T23:59:59.999999Z", client_id="second-client")
        self.begin("after", started_at="2026-10-15T00:00:00Z", actual_model="another-model")
        dates = {"since": "2026-10-14", "until": "2026-10-14"}
        self.assertEqual([row["id"] for row in self.store.recent(dates)], ["last", "first"])
        self.assertEqual(len(self.store.recent({**dates, "client_id": "second-client"})), 1)
        self.assertEqual(len(self.store.recent({"requested_model": "small", "backend": "ollama"})), 4)
        self.assertEqual(len(self.store.recent({"actual_model": "another-model"})), 1)
        groups = self.store.aggregate(dates, ["date", "client_id", "model", "backend"])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["date"], "2026-10-14")
        self.assertEqual(groups[0]["model"], "local-model:latest")
        self.assertEqual(groups[0]["backend"], "ollama")
        self.assertEqual(groups[0]["request_count"], 1)
        self.assertEqual(len(self.store.recent({}, limit=1)), 1)

    def test_backup_is_consistent_restorable_and_refuses_replacement(self):
        self.begin()
        self.store.finish("one", {"status": "success", "output_tokens": 5})
        target = Path(self.directory.name) / "backup.sqlite3"
        self.store.backup(target)
        self.begin("after-backup")
        with self.assertRaises(FileExistsError):
            self.store.backup(target)
        with Store(target) as restored:
            self.assertEqual(len(restored.recent()), 1)
            self.assertEqual(restored.recent()[0]["output_tokens"], 5)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_concurrent_writes_and_repeated_finalization_do_not_double_count(self):
        def request(index):
            request_id = str(index)
            self.begin(request_id, stream=bool(index % 2))
            final = {"status": "success", "total_tokens": 10, "token_source": "measured"}
            self.store.finish(request_id, final)
            self.store.finish(request_id, final)

        with ThreadPoolExecutor(max_workers=8) as workers:
            list(workers.map(request, range(80)))
        result = self.store.aggregate()[0]
        self.assertEqual(result["request_count"], 80)
        self.assertEqual(result["total_tokens"], 800)
        self.assertEqual(result["success_count"], 80)
        self.assertEqual(result["total_token_samples"], 80)

    def test_rejects_content_fields_without_persisting_them(self):
        secret = "private-message-and-authorization-value"
        for field in ("prompt", "messages", "response", "authorization", "api_key", "headers"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.begin(field, **{field: secret})
        self.begin()
        with self.assertRaises(ValueError):
            self.store.finish("one", {"prompt": secret})
        self.assertEqual(len(self.store.recent()), 1)
        self.store.close()
        for path in Path(self.directory.name).glob("usage.sqlite3*"):
            self.assertNotIn(secret.encode(), path.read_bytes())

    def test_filters_and_grouping_cannot_inject_sql(self):
        self.begin()
        for filters in (
            {"client_id OR 1=1": "x"}, {"since": "20261014"},
            {"since": "2026-02-30"}, {"until": "2026-10-14T00:00:00Z"},
            {"since": "2026-10-15", "until": "2026-10-14"},
            {"backend": None}, {"backend": ""},
        ):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                self.store.recent(filters)
        self.assertEqual(self.store.recent({"client_id": "' OR 1=1; --"}), [])
        for groups in (["model; DROP TABLE requests"], ["model", "model"], "model", [[]]):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                self.store.aggregate({}, groups)
        self.assertEqual(len(self.store.recent()), 1)

    def test_invalid_measurements_and_identity_are_rejected(self):
        self.begin()
        for update in (
            {"input_tokens": -1}, {"output_tokens": True}, {"total_tokens": 1.5},
            {"request_bytes": -1}, {"duration_ms": float("nan")},
            {"generation_tps": float("inf")}, {"ttft_ms": -0.1},
            {"status": "invented"}, {"token_source": "estimated"}, {"stream": 1},
            {"id": "changed"}, {"started_at": "2026-10-14T12:30:00Z"},
        ):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.store.finish("one", update)
        for timestamp in ("2026-10-14T12:00:00", "2026-10-14T12:00:00+08:00", "bad"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                self.begin("invalid", started_at=timestamp)
        for limit in (0, -1, 1001, True, "5"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.recent(limit=limit)
        with self.assertRaises(KeyError):
            self.store.finish("absent", {"status": "failed"})

    def test_memory_stores_are_independent_and_close_is_idempotent(self):
        with Store(":memory:") as first, Store(":memory:") as second:
            first.begin({"id": "a", "started_at": "2026-10-14T00:00:00Z"})
            self.assertEqual(len(first.recent()), 1)
            self.assertEqual(second.recent(), [])
        first.close()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            first.recent()


if __name__ == "__main__":
    unittest.main()
