"""MemoryBus durable-queue tests (Phase 3 of stm-ltm-membus).

Stdlib unittest, no broker. Covers the inproc adapter + the Store work_queue:
idempotent publish, claim/ack, nak+backoff retry, dead-letter past max_deliver,
crash recovery (expired-lease reclaim), stage isolation, and get_bus fail-open.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.adapters.inproc_bus import InprocBus  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports.membus import MemoryBus, WorkItem, get_bus  # noqa: E402
from core.store import Store  # noqa: E402


class WorkQueueStoreTests(unittest.TestCase):
    """Store-level queue mechanics, with injected clocks for deterministic timing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.store = Store(get_config().db_path)

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()

    def _enqueue(self, msg_id, stage="distill", now=100.0):
        return self.store.enqueue_work(msg_id=msg_id, stage=stage, project_key="p", ref="r", now=now)

    def test_migration_created_work_queue(self):
        row = self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='work_queue'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_publish_is_idempotent(self):
        self.assertTrue(self._enqueue("m1"))
        self.assertFalse(self._enqueue("m1"))  # dup msg_id ignored
        self.assertEqual(self.store.count_work(stage="distill"), 1)

    def test_claim_increments_attempts_and_leases(self):
        self._enqueue("m1")
        rows = self.store.claim_work("distill", 10, now=100.0, lease_ttl=10.0)
        self.assertEqual(len(rows), 1)
        row = self.store.db.execute("SELECT * FROM work_queue WHERE msg_id='m1'").fetchone()
        self.assertEqual(row["status"], "in_progress")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["lease_expires"], 110.0)

    def test_leased_item_not_reclaimed_before_expiry(self):
        self._enqueue("m1")
        self.store.claim_work("distill", 10, now=100.0, lease_ttl=10.0)
        self.assertEqual(len(self.store.claim_work("distill", 10, now=105.0)), 0)  # still leased

    def test_crash_recovery_reclaims_expired_lease(self):
        self._enqueue("m1")
        self.store.claim_work("distill", 10, now=100.0, lease_ttl=10.0)
        again = self.store.claim_work("distill", 10, now=120.0)  # lease expired -> reclaimable
        self.assertEqual(len(again), 1)

    def test_reclaim_expired_method(self):
        self._enqueue("m1")
        self.store.claim_work("distill", 10, now=100.0, lease_ttl=10.0)
        self.assertEqual(self.store.reclaim_expired(now=120.0), 1)
        self.assertEqual(self.store.count_work(status="pending"), 1)

    def test_nak_reschedules_with_delay(self):
        self._enqueue("m1")
        self.store.claim_work("distill", 10, now=100.0, lease_ttl=10.0)
        self.store.nak_work("m1", delay=50.0, now=100.0)  # retry at 150
        self.assertEqual(len(self.store.claim_work("distill", 10, now=120.0)), 0)  # not due
        self.assertEqual(len(self.store.claim_work("distill", 10, now=160.0)), 1)  # due

    def test_ack_removes_and_dead_keeps(self):
        self._enqueue("m1")
        self._enqueue("m2")
        self.store.ack_work("m1")
        self.store.dead_work("m2")
        self.assertEqual(self.store.count_work(), 1)  # m1 gone, m2 remains
        self.assertEqual(self.store.count_work(status="dead"), 1)

    def test_claim_isolates_by_stage(self):
        self._enqueue("a", stage="distill")
        self._enqueue("b", stage="consolidate")
        rows = self.store.claim_work("distill", 10, now=100.0)
        self.assertEqual([r["msg_id"] for r in rows], ["a"])


class InprocBusTests(unittest.TestCase):
    """Adapter-level behaviour through the MemoryBus port."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        # Immediate retries so the dead-letter path is testable without sleeping.
        # Pin bus=inproc so the ambient LTM_BUS env (a user's settings.json) can't
        # route these through NATS.
        self.cfg = replace(get_config(), bus="inproc", bus_backoff=(0.0,), bus_max_deliver=2)
        self.store = Store(self.cfg.db_path)
        self.bus = InprocBus(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()

    def _item(self, msg_id, stage="distill"):
        return WorkItem(stage=stage, project_key="p", msg_id=msg_id, ref="r")

    def test_publish_pull_ack_roundtrip(self):
        self.bus.publish(self._item("m1"))
        leases = self.bus.pull("distill")
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].item.msg_id, "m1")
        self.assertEqual(leases[0].item.attempts, 1)
        leases[0].ack()
        self.assertEqual(self.store.count_work(), 0)

    def test_nak_then_redelivered(self):
        self.bus.publish(self._item("m1"))
        self.bus.pull("distill")[0].nak()  # attempts 1 < 2 -> retry (delay 0)
        leases = self.bus.pull("distill")
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].item.attempts, 2)

    def test_dead_letter_after_max_deliver(self):
        self.bus.publish(self._item("m1"))
        self.bus.pull("distill")[0].nak()  # attempts 1 -> retry
        self.bus.pull("distill")[0].nak()  # attempts 2 >= max_deliver(2) -> dead
        self.assertEqual(len(self.bus.pull("distill")), 0)
        self.assertEqual(self.store.count_work(status="dead"), 1)

    def test_term_dead_letters_immediately(self):
        self.bus.publish(self._item("m1"))
        self.bus.pull("distill")[0].term()
        self.assertEqual(self.store.count_work(status="dead"), 1)
        self.assertEqual(len(self.bus.pull("distill")), 0)

    def test_get_bus_defaults_to_inproc(self):
        self.assertIsInstance(get_bus(self.cfg, self.store), InprocBus)
        self.assertIsInstance(get_bus(self.cfg, self.store), MemoryBus)

    def test_get_bus_falls_open_to_inproc_when_nats_unavailable(self):
        # bus=nats but the server is unreachable (dead port) -> fail open to inproc.
        # Deterministic whether or not nats-py is installed: ImportError OR connect
        # failure both fall through to inproc.
        cfg_nats = replace(self.cfg, bus="nats", nats_url="nats://127.0.0.1:1")
        self.assertIsInstance(get_bus(cfg_nats, self.store), InprocBus)


if __name__ == "__main__":
    unittest.main()
