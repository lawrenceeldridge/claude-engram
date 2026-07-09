"""Sensory register (A-S intake) — the table, Store CRUD, the decay sweep, and the pure
attention gate (should_promote). Stdlib unittest; no network, no embedder needed.

Phase 2 (foundation) of the unified sensory-register rebuild: this covers the *structure*
(the register table + its Repository methods) and the one pure *control process* (attention).
Intake hooks (visual/verbal) and promotion into the durable store land in later phases.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.domain.sensory import should_promote  # noqa: E402
from core.store import Store  # noqa: E402

# The `sensory` table schema an earlier, later-reverted build shipped in the same migration slot
# (no `modality` / `decayed_at`). A database stamped by that build must be reconciled on open.
_LEGACY_SENSORY_DDL = (
    "CREATE TABLE sensory ("
    "  id TEXT PRIMARY KEY, project_key TEXT NOT NULL, session_id TEXT, url TEXT, text TEXT,"
    "  created_at REAL, glance_count INTEGER DEFAULT 1, attended INTEGER DEFAULT 0, promoted_fact_id TEXT"
    ");"
)


class SensoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.db")
        self.pk = "proj"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_migration_created_sensory_table(self):
        cols = {r[1] for r in self.store.db.execute("PRAGMA table_info(sensory)")}
        self.assertEqual(
            cols,
            {"id", "project_key", "modality", "observation_id", "url", "text", "attended", "created_at", "decayed_at"},
        )

    def test_add_and_rows_roundtrip(self):
        sid = self.store.add_sensory(self.pk, "visual", "heading 'Login'", url="https://x/app", now=100.0)
        rows = self.store.sensory_rows(self.pk)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["id"], sid)
        self.assertEqual(r["modality"], "visual")
        self.assertEqual(r["url"], "https://x/app")
        self.assertEqual(r["text"], "heading 'Login'")
        self.assertEqual(r["attended"], 0)
        self.assertIsNone(r["decayed_at"])

    def test_idempotent_same_content_refreshes_not_duplicates(self):
        a = self.store.add_sensory(self.pk, "visual", "same text", url="u", now=1.0)
        b = self.store.add_sensory(self.pk, "visual", "same text", url="u", now=2.0)  # re-perceive
        self.assertEqual(a, b)  # same content -> same id
        rows = self.store.sensory_rows(self.pk)
        self.assertEqual(len(rows), 1)  # no duplicate
        self.assertEqual(rows[0]["created_at"], 2.0)  # recency refreshed

    def test_distinct_modality_or_url_is_a_new_perception(self):
        self.store.add_sensory(self.pk, "visual", "t", url="u1", now=1.0)
        self.store.add_sensory(self.pk, "visual", "t", url="u2", now=1.0)
        self.store.add_sensory(self.pk, "verbal", "t", now=1.0)
        self.assertEqual(len(self.store.sensory_rows(self.pk)), 3)

    def test_mark_attended(self):
        sid = self.store.add_sensory(self.pk, "visual", "t", url="u", now=1.0)
        self.assertEqual(self.store.sensory_rows(self.pk)[0]["attended"], 0)
        self.store.mark_attended(sid)
        self.assertEqual(self.store.sensory_rows(self.pk)[0]["attended"], 1)

    def test_readd_revives_a_decayed_tombstone(self):
        sid = self.store.add_sensory(self.pk, "visual", "t", url="u", now=100.0)
        self.store.sweep_sensory(self.pk, capacity=0, ttl_seconds=60, now=1000.0)  # tombstone it
        self.assertEqual(self.store.sensory_rows(self.pk), [])
        again = self.store.add_sensory(self.pk, "visual", "t", url="u", now=1001.0)  # re-perceive
        self.assertEqual(again, sid)
        self.assertEqual(len(self.store.sensory_rows(self.pk)), 1)  # back in the live register

    def test_sweep_decays_then_purges_unattended(self):
        self.store.add_sensory(self.pk, "visual", "old", url="u", now=100.0)
        d1 = self.store.sweep_sensory(self.pk, capacity=0, ttl_seconds=60, now=1000.0)
        self.assertEqual(d1, 1)
        self.assertEqual(self.store.sensory_rows(self.pk), [])  # left the live register
        self.assertEqual(len(self.store.sensory_rows(self.pk, include_decayed=True)), 1)  # tombstone lingers
        self.store.sweep_sensory(self.pk, capacity=0, ttl_seconds=60, now=2000.0)  # past tombstone ttl
        self.assertEqual(self.store.sensory_rows(self.pk, include_decayed=True), [])  # hard-purged

    def test_sweep_keeps_unattended_within_ttl(self):
        self.store.add_sensory(self.pk, "visual", "fresh", url="u", now=1000.0)
        self.assertEqual(self.store.sweep_sensory(self.pk, capacity=0, ttl_seconds=60, now=1030.0), 0)
        self.assertEqual(len(self.store.sensory_rows(self.pk)), 1)

    def test_sweep_protects_attended(self):
        sid = self.store.add_sensory(self.pk, "visual", "kept", url="u", now=100.0)
        self.store.mark_attended(sid)
        # both limbs would otherwise catch it (past ttl AND beyond capacity=0-effective); attended wins
        self.assertEqual(self.store.sweep_sensory(self.pk, capacity=1, ttl_seconds=60, now=1000.0), 0)
        self.assertEqual(len(self.store.sensory_rows(self.pk)), 1)

    def test_sweep_capacity_displaces_oldest_unattended(self):
        for i in range(5):
            self.store.add_sensory(self.pk, "visual", f"p{i}", url=f"u{i}", now=100.0 + i)
        d = self.store.sweep_sensory(self.pk, capacity=3, ttl_seconds=0, now=110.0)
        self.assertEqual(d, 2)  # 5 live - capacity 3
        live = {r["text"] for r in self.store.sensory_rows(self.pk)}
        self.assertEqual(live, {"p2", "p3", "p4"})  # newest 3 kept

    def test_sensory_counts_live_only(self):
        self.store.add_sensory(self.pk, "visual", "a", url="u1", now=100.0)
        self.store.add_sensory(self.pk, "visual", "b", url="u2", now=101.0)
        self.store.sweep_sensory(self.pk, capacity=1, ttl_seconds=0, now=110.0)  # decays the older
        self.assertEqual(self.store.sensory_counts().get(self.pk), 1)

    def test_delete_sensory(self):
        sid = self.store.add_sensory(self.pk, "visual", "t", url="u", now=1.0)
        self.assertEqual(self.store.delete_sensory(sid), 1)
        self.assertEqual(self.store.sensory_rows(self.pk), [])
        self.assertEqual(self.store.delete_sensory(""), 0)  # no-op on empty id


class ShouldPromoteTests(unittest.TestCase):
    """The pure A-S attention gate: attended + live -> promote; nothing else."""

    def test_attended_and_live_promotes(self):
        self.assertTrue(should_promote({"attended": 1, "decayed_at": None}))

    def test_unattended_does_not_promote(self):
        self.assertFalse(should_promote({"attended": 0, "decayed_at": None}))

    def test_decayed_does_not_promote(self):
        self.assertFalse(should_promote({"attended": 1, "decayed_at": 123.0}))

    def test_accepts_bool_attended(self):
        # dict-only inputs, no Store / no clock — proves it is a pure Functional-Core decision
        self.assertTrue(should_promote({"attended": True, "decayed_at": None}))
        self.assertFalse(should_promote({"attended": False, "decayed_at": None}))


class MigrationReconcileTests(unittest.TestCase):
    """Regression: a DB carrying the earlier (reverted) sensory schema at the same migration slot
    must be reconciled on open, not left with a stale table that crashes sensory_counts/_stats."""

    def test_legacy_sensory_table_is_reconciled_on_open(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "memory.db"
            con = sqlite3.connect(db_path)
            con.executescript(_LEGACY_SENSORY_DDL)
            con.execute(
                "INSERT INTO sensory (id, project_key, session_id, url, text, created_at, attended) "
                "VALUES ('x', 'proj', 's', 'https://x', 'old perception', 1.0, 1)"
            )
            con.execute("PRAGMA user_version = 16")  # stamped by the buggy build
            con.commit()
            con.close()

            store = Store(db_path)  # must migrate cleanly, not raise
            try:
                cols = {r[1] for r in store.db.execute("PRAGMA table_info(sensory)")}
                self.assertIn("decayed_at", cols)  # reconciled to the current schema
                self.assertIn("modality", cols)
                # the previously-crashing queries now work
                self.assertEqual(store.sensory_counts(), {})  # stale rows dropped (register is transient)
                self.assertEqual(
                    store.sensory_stats("proj"), {"live": 0, "attended": 0, "visual": 0, "verbal": 0}
                )
                # and the register is usable again
                store.add_sensory("proj", "visual", "new", url="https://y", now=100.0)
                self.assertEqual(len(store.sensory_rows("proj")), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
