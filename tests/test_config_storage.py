"""Durable default storage across checkout replacement and process restarts."""

from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_llm_gateway.config import Config, LegacyDatabaseError
from local_llm_gateway.storage import Store


CONFIG = """[backends.ollama]
type = "ollama"
url = "http://127.0.0.1:11434"
[models.chat]
backend = "ollama"
model = "tiny:local"
[clients.demo]
default_model = "chat"
"""


class PersistentConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "user-data"
        environment = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data)})
        environment.start()
        self.addCleanup(environment.stop)

    def config(self, directory, server=""):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "gateway.toml"
        path.write_text(server + CONFIG, encoding="utf-8")
        return path

    def record(self, store):
        store.begin({"id": "durable", "started_at": "2026-10-29T12:00:00Z",
                     "client_id": "demo", "actual_model": "tiny:local",
                     "request_bytes": 80, "stream": False})
        store.finish("durable", {"status": "success", "input_tokens": 4,
                                  "output_tokens": 3, "total_tokens": 7,
                                  "token_source": "measured"})

    def test_replaced_checkout_and_restart_use_identical_database(self):
        first = self.root / "first-checkout"
        original = Config.load(self.config(first)).database
        self.assertEqual(original, self.data / "local-llm-gateway" / "usage.sqlite3")
        with Store(original) as store:
            self.record(store)
            rows = store.recent()
        shutil.rmtree(first)
        second = Config.load(self.config(self.root / "replacement-checkout")).database
        self.assertEqual(second, original)
        with Store(second) as store:
            self.assertEqual(store.recent(), rows)
            self.assertEqual(store.aggregate()[0]["total_tokens"], 7)
        with Store(second) as restarted:
            self.assertEqual(restarted.recent(), rows)

    def test_unset_empty_or_relative_xdg_uses_absolute_home_data_path(self):
        config = self.config(self.root / "checkout")
        home = self.root / "home"
        with patch("local_llm_gateway.config.Path.home", return_value=home):
            for value in (None, "", "relative-data"):
                with self.subTest(value=value):
                    if value is None:
                        os.environ.pop("XDG_DATA_HOME", None)
                    else:
                        os.environ["XDG_DATA_HOME"] = value
                    self.assertEqual(Config.load(config).database,
                                     home / ".local/share/local-llm-gateway/usage.sqlite3")

    def test_explicit_paths_keep_existing_configuration_semantics(self):
        directory = self.root / "checkout"
        relative = self.config(directory, '[server]\ndatabase = ".data/existing.sqlite3"\n')
        self.assertEqual(Config.load(relative).database, directory / ".data/existing.sqlite3")
        absolute = self.root / "chosen.sqlite3"
        explicit = self.config(directory, f'[server]\ndatabase = "{absolute}"\n')
        self.assertEqual(Config.load(explicit).database, absolute)
        home = self.root / "home"
        expanded = self.config(directory, '[server]\ndatabase = "~/chosen.sqlite3"\n')
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(Config.load(expanded).database, home / "chosen.sqlite3")

    def test_legacy_default_requires_backup_migration_before_switching(self):
        directory = self.root / "checkout"
        config = self.config(directory)
        legacy = directory / ".data/usage.sqlite3"
        with Store(legacy) as store:
            self.record(store)
            rows = store.recent()
        with self.assertRaises(LegacyDatabaseError):
            Config.load(config)
        target = self.data / "local-llm-gateway/usage.sqlite3"
        self.assertFalse(target.exists())
        target.parent.mkdir(parents=True)
        self.config(directory, '[server]\ndatabase = ".data/usage.sqlite3"\n')
        process = subprocess.run(
            [sys.executable, "-m", "local_llm_gateway.cli", "--config", str(config), "backup", str(target)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("Backup created", process.stdout)
        self.config(directory)
        self.assertEqual(Config.load(config).database, target)
        with Store(target) as migrated:
            self.assertEqual(migrated.recent(), rows)
        with Store(legacy) as preserved:
            self.assertEqual(preserved.recent(), rows)

    def test_cli_explains_legacy_database_and_creates_no_empty_replacement(self):
        directory = self.root / "checkout"
        config = self.config(directory)
        with Store(directory / ".data/usage.sqlite3") as store:
            self.record(store)
        process = subprocess.run(
            [sys.executable, "-m", "local_llm_gateway.cli", "--config", str(config), "serve"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(process.returncode, 1)
        self.assertIn("legacy .data/usage.sqlite3", process.stderr)
        self.assertIn("docs/OWNER_GUIDE.md", process.stderr)
        self.assertFalse(self.data.exists())

    def test_corrupt_database_is_not_reset_or_replaced_with_a_fallback(self):
        config = Config.load(self.config(self.root / "checkout"))
        config.database.parent.mkdir(parents=True)
        original = b"existing but damaged database -- preserve for recovery"
        config.database.write_bytes(original)
        with self.assertRaises(sqlite3.DatabaseError):
            Store(config.database)
        self.assertEqual(config.database.read_bytes(), original)
        self.assertFalse((self.root / "checkout/.data").exists())

    def test_nonpersistent_or_invalid_database_values_are_refused(self):
        import tomllib
        data = tomllib.loads(CONFIG)
        for value in (":memory:", "", "  ", None, 0, False):
            with self.subTest(value=value):
                data["server"] = {"database": value}
                with self.assertRaises(ValueError):
                    Config.from_dict(data, self.root)
