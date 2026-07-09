"""Config data-dir resolution — stdlib unittest, no external deps.

Guards the standalone-viewer regression: a CLI/viewer run with no CLAUDE_PLUGIN_DATA
leaves an *empty* ``data/engram/memory.db`` behind, which used to shadow the live
marketplace-qualified sibling (``data/engram-<marketplace>``) on every later run.
``_data_dir()`` now treats an empty default as "no real store" and adopts the newest
non-empty sibling instead.

Run: python3 -m unittest discover -s plugins/engram/tests  (from repo root)
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config, service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402


class HasMemoriesTests(unittest.TestCase):
    """The predicate that distinguishes a live store from an empty/absent one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # A scratch data dir so building fixture DBs never touches the real home.
        os.environ["ENGRAM_DATA_DIR"] = str(self.dir / "scratch")
        self.cfg = replace(get_config(), distiller="heuristic")
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _make_db(self, path: Path, *, with_fact: bool) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path)
        try:
            if with_fact:
                service.add_facts(store, self.embedder, self.cfg, self.project, "s1", ["a durable fact worth keeping"])
        finally:
            store.close()
        return path

    def test_absent_file_has_no_memories(self):
        self.assertFalse(config._has_memories(self.dir / "nope" / "memory.db"))

    def test_empty_schema_db_has_no_memories(self):
        db = self._make_db(self.dir / "empty" / "memory.db", with_fact=False)
        self.assertFalse(config._has_memories(db))

    def test_db_with_a_fact_has_memories(self):
        db = self._make_db(self.dir / "live" / "memory.db", with_fact=True)
        self.assertTrue(config._has_memories(db))

    def test_corrupt_file_fails_closed(self):
        bad = self.dir / "corrupt" / "memory.db"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"this is not a sqlite database")
        self.assertFalse(config._has_memories(bad))  # sqlite error -> not adopted, no raise


class DataDirResolutionTests(unittest.TestCase):
    """``_data_dir()`` resolution once env overrides are absent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.base = self.home / ".claude" / "plugins" / "data"
        self.base.mkdir(parents=True, exist_ok=True)
        # Build the two fixture DBs with a scratch env so setup never hits the fake home.
        os.environ["ENGRAM_DATA_DIR"] = str(self.home / "scratch")
        self.cfg = replace(get_config(), distiller="heuristic")
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _make_db(self, path: Path, *, with_fact: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        store = Store(path)
        try:
            if with_fact:
                service.add_facts(store, self.embedder, self.cfg, self.project, "s1", ["a durable fact worth keeping"])
        finally:
            store.close()

    def _resolve_without_env(self) -> Path:
        """Run _data_dir() with both env overrides removed and home pointed at the fixture."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENGRAM_DATA_DIR", None)
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
            with mock.patch.object(config.Path, "home", return_value=self.home):
                return config._data_dir()

    def test_empty_default_does_not_shadow_live_sibling(self):
        # The regression: an empty default left by a prior standalone run must NOT win.
        self._make_db(self.base / "engram" / "memory.db", with_fact=False)
        self._make_db(self.base / "engram-claude-engram" / "memory.db", with_fact=True)
        self.assertEqual(self._resolve_without_env(), self.base / "engram-claude-engram")

    def test_absent_default_still_adopts_sibling(self):
        # Original behaviour preserved: no default at all -> newest non-empty sibling.
        self._make_db(self.base / "engram-claude-engram" / "memory.db", with_fact=True)
        self.assertEqual(self._resolve_without_env(), self.base / "engram-claude-engram")

    def test_populated_default_wins_over_sibling(self):
        # A real default store is used directly; siblings are only a fallback.
        self._make_db(self.base / "engram" / "memory.db", with_fact=True)
        self._make_db(self.base / "engram-claude-engram" / "memory.db", with_fact=True)
        self.assertEqual(self._resolve_without_env(), self.base / "engram")

    def test_no_store_anywhere_falls_back_to_default(self):
        # Nothing live and no empty default either -> create/return the bare default.
        self.assertEqual(self._resolve_without_env(), self.base / "engram")

    def test_env_override_wins_before_auto_resolution(self):
        self._make_db(self.base / "engram-claude-engram" / "memory.db", with_fact=True)
        forced = self.home / "forced"
        with mock.patch.dict(os.environ, {"ENGRAM_DATA_DIR": str(forced)}, clear=False):
            with mock.patch.object(config.Path, "home", return_value=self.home):
                self.assertEqual(config._data_dir(), forced)


if __name__ == "__main__":
    unittest.main()
