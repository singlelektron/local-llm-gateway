"""Strict administrator-owned routing configuration."""
from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def loopback_host(host):
    if host == "localhost":
        return "127.0.0.1"
    try:
        if ipaddress.ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError("Only literal loopback addresses or localhost are supported")


def local_url(value):
    if not isinstance(value, str):
        raise ValueError("Backend URL must be a string")
    u = urlsplit(value)
    if u.scheme != "http" or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError("Backend URL must be a plain loopback HTTP origin")
    host = loopback_host(u.hostname)
    if ":" in host:
        host = f"[{host}]"
    port = u.port if u.port is not None else 80
    if not 1 <= port <= 65535:
        raise ValueError("Invalid backend port")
    return f"http://{host}:{port}"


def only_keys(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError("Unsupported configuration fields")


def positive_int(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Configuration integer outside supported range")
    return value


class LegacyDatabaseError(ValueError):
    """An old implicit database needs an explicit migration before switching."""


def database_path(server, base):
    if "database" in server:
        value = server["database"]
        if not isinstance(value, str) or not value.strip() or value == ":memory:":
            raise ValueError("Configure a persistent database file")
        return Path(base) / Path(value).expanduser()
    # Ignore relative XDG values so a changed working directory cannot select a
    # fresh database. Configured paths retain their existing relative semantics.
    xdg = os.environ.get("XDG_DATA_HOME", "")
    root = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "share"
    target = root / "local-llm-gateway" / "usage.sqlite3"
    if (Path(base) / ".data" / "usage.sqlite3").exists() and not target.exists():
        raise LegacyDatabaseError("Migrate the legacy statistics database first")
    return target


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    database: Path
    timeout_seconds: int
    max_concurrent: int
    backends: dict
    models: dict
    clients: dict

    @classmethod
    def from_dict(cls, data, base=Path(".")):
        only_keys(data, {"server", "backends", "models", "clients"})
        server = data.get("server", {})
        only_keys(server, {"host", "port", "database", "timeout_seconds", "max_concurrent"})
        backends, models, clients = (data.get(k, {}) for k in ("backends", "models", "clients"))
        for table in (backends, models, clients):
            if not isinstance(table, dict) or not table or not all(LABEL.fullmatch(k) for k in table):
                raise ValueError("Configure named backends, models and clients")
        normalized = {}
        for name, backend in backends.items():
            only_keys(backend, {"type", "url"})
            if backend.get("type") != "ollama":
                raise ValueError("Only Ollama is implemented")
            normalized[name] = {"type": "ollama", "url": local_url(backend.get("url"))}
        for model in models.values():
            only_keys(model, {"backend", "model"})
            if model.get("backend") not in normalized:
                raise ValueError("Model references an unknown backend")
            name = model.get("model")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}", name) or "://" in name:
                raise ValueError("Invalid configured model name")
        for client in clients.values():
            only_keys(client, {"default_model"})
            if client.get("default_model") not in models:
                raise ValueError("Client default references an unknown alias")
        return cls(loopback_host(server.get("host", "127.0.0.1")),
                   positive_int(server.get("port", 8787), 65535),
                   database_path(server, base),
                   positive_int(server.get("timeout_seconds", 180), 3600),
                   positive_int(server.get("max_concurrent", 4), 128),
                   normalized, models, clients)

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        with path.open("rb") as f:
            return cls.from_dict(tomllib.load(f), path.parent)
