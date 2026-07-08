"""Sensory register — Phase 1: migration, store round-trips, pure should_promote, config inert.

All stdlib, no network. The full recall-isolation test (via recall_structured) lands in
Phase 2 once recording is wired; here we assert the store-level isolation (sensory rows
never appear in the facts/recall query).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import get_config
from core.domain.sensory import should_promote
from core.store import Store


class SensoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="engram-sensory-")
        self.store = Store(Path(self.tmp) / "m.db")
        self.pk = "proj"

    def tearDown(self):
        self.store.close()

    def test_migration_created_sensory_table(self):
        cols = {r[1] for r in self.store.db.execute("PRAGMA table_info(sensory)")}
        self.assertEqual(
            cols,
            {
                "id",
                "project_key",
                "session_id",
                "url",
                "text",
                "created_at",
                "glance_count",
                "attended",
                "promoted_fact_id",
            },
        )

    def test_add_is_upsert_bumping_glance_count(self):
        a = self.store.add_sensory(self.pk, "s1", "https://ex.com/login", "- heading X")
        b = self.store.add_sensory(self.pk, "s1", "https://ex.com/login", "- heading X v2")
        self.assertEqual(a, b)  # same (session, url) → same row
        rows = self.store.sensory_rows(self.pk)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["glance_count"], 2)
        self.assertEqual(rows[0]["text"], "- heading X v2")  # refreshed on re-glance

    def test_sensory_rows_newest_first(self):
        self.store.add_sensory(self.pk, "s1", "u1", "a", now=1000.0)
        self.store.add_sensory(self.pk, "s1", "u2", "b", now=1001.0)
        self.assertEqual(self.store.sensory_rows(self.pk)[0]["url"], "u2")

    def test_mark_attended(self):
        sid = self.store.add_sensory(self.pk, "s1", "u1", "a")
        self.store.mark_attended(sid, "fact123")
        row = self.store.sensory_rows(self.pk)[0]
        self.assertEqual(row["attended"], 1)
        self.assertEqual(row["promoted_fact_id"], "fact123")

    def test_sweep_ttl_hard_deletes_old(self):
        now = 1_000_000.0
        self.store.add_sensory(self.pk, "s1", "old", "a", now=now - 10_000)
        self.store.add_sensory(self.pk, "s1", "new", "b", now=now)
        deleted = self.store.sweep_sensory(self.pk, capacity=0, ttl_seconds=900, now=now)
        self.assertEqual(deleted, 1)
        self.assertEqual([r["url"] for r in self.store.sensory_rows(self.pk)], ["new"])

    def test_sweep_capacity_keeps_newest(self):
        for i in range(5):
            self.store.add_sensory(self.pk, "s1", f"u{i}", "x", now=1000.0 + i)
        deleted = self.store.sweep_sensory(self.pk, capacity=2, ttl_seconds=0)
        self.assertEqual(deleted, 3)
        self.assertEqual({r["url"] for r in self.store.sensory_rows(self.pk)}, {"u3", "u4"})

    def test_store_level_recall_isolation(self):
        # Sensory lives in its own table; the facts/recall query never touches it.
        self.store.add_sensory(self.pk, "s1", "u1", "secret sensory text")
        self.assertEqual(self.store.active_rows_for_project(self.pk), [])


class ShouldPromoteTests(unittest.TestCase):
    def test_promotes_at_or_above_threshold_when_unattended(self):
        self.assertTrue(should_promote({"glance_count": 2, "attended": 0}, promote_after=2))
        self.assertTrue(should_promote({"glance_count": 5, "attended": 0}, promote_after=2))

    def test_not_below_threshold(self):
        self.assertFalse(should_promote({"glance_count": 1, "attended": 0}, promote_after=2))

    def test_never_repromotes_attended(self):
        self.assertFalse(should_promote({"glance_count": 9, "attended": 1}, promote_after=2))


class SensoryConfigTests(unittest.TestCase):
    def test_defaults_off_and_inert(self):
        cfg = get_config()
        self.assertFalse(cfg.sensory)
        self.assertEqual(cfg.sensory_promote_after, 2)
        self.assertGreater(cfg.sensory_capacity, 0)


if __name__ == "__main__":
    unittest.main()
