"""Heuristic-fallback recovery: park degraded captures, re-distil them later.

Recovery now runs through the durable MemoryBus 'rescue' stage (design §6.4): a
degraded capture publishes a rescue work item; a later healthy session drains and
re-distils it. Verified without a live LLM by stubbing the distiller.
Run: python3 -m unittest discover -s plugins/engram/tests
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports.distill import DistilledFact  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import _SCHEMA_VERSION, Store  # noqa: E402


class _StubDistiller:
    def __init__(self, records):
        self._records = records

    def distill(self, text, existing):
        return [DistilledFact(**r) for r in self._records]

    def summarize(self, text):
        return None


def _titled():
    return [{"text": "uses gemma3 for distillation", "title": "Distiller", "type": "feature", "degraded": False}]


def _degraded():
    return [{"text": "some raw line", "type": "discovery", "degraded": True}]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        os.environ["ENGRAM_DISTILLER"] = "ollama"  # an LLM distiller, so recovery is active
        # Pin bus=inproc so an ambient ENGRAM_BUS (a user's settings.json) can't route the
        # recovery queue through NATS instead of the local sqlite work_queue.
        self.cfg = replace(get_config(), bus="inproc")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()
        for k in ("ENGRAM_DATA_DIR", "ENGRAM_DISTILLER"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def _rescue_count(self):
        return self.store.count_work(stage="rescue")

    def test_migration_created_queues(self):
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], _SCHEMA_VERSION)
        names = {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master")}
        self.assertIn("work_queue", names)

    def test_delete_facts_keeps_fts_in_sync(self):
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", ["alpha widget deploy"])
        fid = self.store.fact_id("p", "alpha widget deploy")
        self.assertEqual(len(self.store.fts_search("p", "widget")), 1)
        self.store.delete_facts([fid])
        self.assertEqual(self.store.fts_search("p", "widget"), [])
        self.assertIsNone(self.store.get(fid))

    def test_capture_publishes_rescue_on_degraded(self):
        with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(_degraded())):
            service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", "a failing delta")
        self.assertEqual(self._rescue_count(), 1)

    def test_capture_does_not_publish_when_titled(self):
        with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(_titled())):
            service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", "a good delta")
        self.assertEqual(self._rescue_count(), 0)

    def test_capture_publish_is_idempotent(self):
        # Same delta degrading twice must not stack duplicate rescue items (msg_id dedup).
        with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(_degraded())):
            service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", "same delta")
            service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", "same delta")
        self.assertEqual(self._rescue_count(), 1)

    def test_rescue_replaces_heuristic_with_titled(self):
        with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(_degraded())):
            service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", "raw delta text")
        heuristic_id = self.store.fact_id("p", "some raw line")
        self.assertIsNotNone(self.store.get(heuristic_id))
        self.assertEqual(self._rescue_count(), 1)
        # A now-healthy session drains the queue: heuristic fact replaced by the titled one.
        with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(_titled())):
            recovered = service.rescue(self.store, self.embedder, self.cfg)
        self.assertEqual(recovered, 1)
        self.assertIsNone(self.store.get(heuristic_id))  # heuristic fact gone
        self.assertIsNotNone(self.store.get(self.store.fact_id("p", "uses gemma3 for distillation")))
        self.assertEqual(self._rescue_count(), 0)  # acked -> removed

    def test_rescue_naks_when_still_failing(self):
        with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(_degraded())):
            service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", "raw")
            recovered = service.rescue(self.store, self.embedder, self.cfg)
        self.assertEqual(recovered, 0)
        self.assertEqual(self._rescue_count(), 1)  # nak'd back to the queue for a later retry

    def test_rescue_noop_for_heuristic_distiller(self):
        with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(_degraded())):
            service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", "raw")
        os.environ["ENGRAM_DISTILLER"] = "heuristic"
        cfg = replace(get_config(), bus="inproc")
        self.assertEqual(service.rescue(self.store, self.embedder, cfg), 0)
        self.assertEqual(self._rescue_count(), 1)  # left intact — a heuristic install never recovers

    def test_v11_migrates_legacy_redistill_to_bus(self):
        # A pre-v11 store's pending_redistill rows must move into the 'rescue' queue.
        p = Path(self.tmp.name) / "legacy.db"
        con = sqlite3.connect(p)
        con.executescript(
            "CREATE TABLE pending_redistill (id INTEGER PRIMARY KEY AUTOINCREMENT, project_key TEXT NOT NULL, "
            "session_id TEXT, text TEXT NOT NULL, fact_ids TEXT, attempts INTEGER DEFAULT 0, created_at REAL);"
        )
        con.execute(
            "INSERT INTO pending_redistill (project_key, session_id, text, fact_ids, created_at) "
            "VALUES ('p', 's1', 'parked delta', '[\"old1\"]', 1.0)"
        )
        con.execute("PRAGMA user_version = 0")  # force the full ladder to replay
        con.commit()
        con.close()

        store = Store(p)  # runs migrations incl. _v11
        self.assertEqual(store.count_work(stage="rescue"), 1)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM pending_redistill").fetchone()[0], 0)
        store.close()


if __name__ == "__main__":
    unittest.main()
