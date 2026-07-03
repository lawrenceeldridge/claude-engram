"""NATS JetStream adapter integration tests (Phase 5).

Gated: run only when nats-py is installed AND a live JetStream server is reachable
via LTM_TEST_NATS_URL. Skipped by the default stdlib suite (no nats-py, no server).

To run:
  docker run -d --rm -p 4223:4222 nats:latest --jetstream
  LTM_TEST_NATS_URL=nats://localhost:4223 <python-with-nats-py> -m unittest tests.test_nats_bus
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import nats  # noqa: F401

    _HAVE_NATS = True
except ImportError:
    _HAVE_NATS = False

_URL = os.environ.get("LTM_TEST_NATS_URL")
_REASON = "requires nats-py + a live NATS at LTM_TEST_NATS_URL"


@unittest.skipUnless(_HAVE_NATS and _URL, _REASON)
class NatsBusIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stream = "LTM_TEST_" + os.urandom(4).hex()  # unique per test
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        os.environ["LTM_NATS_URL"] = _URL
        os.environ["LTM_NATS_STREAM"] = self.stream
        from core.config import get_config

        self.cfg = replace(get_config(), bus_max_deliver=2, bus_backoff=(0.0,))
        from core.adapters.nats_bus import NatsBus

        self.bus = NatsBus(self.cfg)

    def tearDown(self):
        # Delete the stream — JetStream forbids two streams over the same subjects, so a
        # leftover would break the next test's stream creation. Force-connect first
        # (some tests create the stream via a separate bus, leaving self.bus lazy).
        try:
            self.bus.connect()
            self.bus._run(self.bus._js.delete_stream(self.stream))
        except Exception:
            pass
        self.bus.close()
        for k in ("LTM_DATA_DIR", "LTM_NATS_URL", "LTM_NATS_STREAM"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def _item(self, msg_id, payload='{"x": 1}', stage="rescue"):
        from core.ports.membus import WorkItem

        return WorkItem(stage=stage, project_key="p", msg_id=msg_id, payload=payload)

    def test_publish_pull_ack(self):
        self.bus.publish(self._item("m1"))
        leases = self.bus.pull("rescue", 10)
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0].item.msg_id, "m1")
        self.assertEqual(leases[0].item.attempts, 1)
        self.assertEqual(leases[0].item.payload, '{"x": 1}')
        leases[0].ack()
        self.assertEqual(len(self.bus.pull("rescue", 10)), 0)

    def test_publish_is_idempotent(self):
        self.bus.publish(self._item("dup"))
        self.bus.publish(self._item("dup"))  # deduped by Nats-Msg-Id
        leases = self.bus.pull("rescue", 10)
        self.assertEqual(len(leases), 1)
        leases[0].ack()

    def test_nak_redelivers_then_stops_past_max_deliver(self):
        self.bus.publish(self._item("r1"))
        l1 = self.bus.pull("rescue", 10)
        self.assertEqual(l1[0].item.attempts, 1)
        l1[0].nak()
        l2 = self.bus.pull("rescue", 10)
        self.assertEqual(l2[0].item.attempts, 2)  # redelivered
        l2[0].nak()  # attempts now == max_deliver(2)
        time.sleep(0.2)
        self.assertEqual(len(self.bus.pull("rescue", 10)), 0)  # no more redelivery

    def test_stage_isolation(self):
        self.bus.publish(self._item("a", stage="rescue"))
        self.bus.publish(self._item("b", stage="distill"))
        r = self.bus.pull("rescue", 10)
        self.assertEqual([lease.item.msg_id for lease in r], ["a"])
        r[0].ack()
        d = self.bus.pull("distill", 10)
        self.assertEqual([lease.item.msg_id for lease in d], ["b"])
        d[0].ack()

    def test_get_bus_returns_nats_when_up(self):
        os.environ["LTM_BUS"] = "nats"
        try:
            from core.adapters.nats_bus import NatsBus
            from core.config import get_config
            from core.ports.membus import get_bus

            bus = get_bus(get_config(), None)
            self.assertIsInstance(bus, NatsBus)
            bus.close()
        finally:
            os.environ.pop("LTM_BUS", None)


if __name__ == "__main__":
    unittest.main()
