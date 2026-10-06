"""Keyword indexes that stay whole: atomic FTS migrations, coverage, and the detached repair.

An older build's ladder replay once left ``facts_fts`` existing but empty (its ``_v6`` committed the
DROP / CREATE apart from the ``rebuild``) while its triggers kept writing: the keyword channel went
blind and updates failed as "database disk image is malformed". ``_fts_built`` makes create +
backfill one transaction; ``Store.fts_coverage`` / ``repair_fts`` detect and heal what older code
can still do; ``health.fts_check`` reports it.
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from _harness import temp_data_dir

from core import health
from core.store import _FTS_SCHEMA, Store, _fts_built

TEXTS = ["the deploy pipeline ships to aws lambda", "signing keys rotate every ninety days", "int8 vectors stay small"]


def _fts_ok(db: sqlite3.Connection, fts: str) -> bool:
    try:
        db.execute(f"INSERT INTO {fts}({fts}, rank) VALUES ('integrity-check', 1)")
    except sqlite3.DatabaseError:
        return False
    return True


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(temp_data_dir(self).name) / "memory.db")
        self.addCleanup(self.store.close)
        for text in TEXTS:
            self.store.add(
                project={"key": "p", "label": "p", "path": "/x"},
                session_id="s",
                kind="fact",
                text=text,
                vec_int8=b"\x00" * 8,
                scale=1.0,
                dim=8,
                vec_bits=b"",
                importance=0.5,
            )

    def empty_index(self, fts: str = "facts_fts") -> None:
        with self.store.db:
            self.store.db.execute(f"INSERT INTO {fts}({fts}) VALUES ('delete-all')")


class CoverageTests(_Fixture):
    def test_a_healthy_store_is_fully_covered_and_repair_writes_nothing(self):
        self.assertEqual(self.store.fts_coverage(), {"facts_fts": (3, 3), "chunks_fts": (0, 0)})
        before = self.store.db.total_changes
        self.assertEqual(self.store.repair_fts(), [])
        self.assertEqual(self.store.db.total_changes, before)

    def test_an_emptied_index_is_detected_and_rebuilt_whole(self):
        self.empty_index()
        self.assertEqual(self.store.fts_coverage()["facts_fts"], (0, 3))
        self.assertFalse(_fts_ok(self.store.db, "facts_fts"))  # the damage the incident left
        self.assertEqual(self.store.repair_fts(), [("facts_fts", 0, 3)])
        self.assertEqual(self.store.fts_coverage()["facts_fts"], (3, 3))
        self.assertTrue(_fts_ok(self.store.db, "facts_fts"))
        self.assertEqual(len(self.store.fts_search("p", "lambda")), 1)  # the keyword channel sees again


class HealthTests(_Fixture):
    def test_the_check_is_ok_on_a_covered_store_and_warns_on_a_gap(self):
        self.assertEqual(health.fts_check(self.store).state, "ok")
        self.empty_index()
        check = health.fts_check(self.store)
        self.assertEqual(check.state, "warn")
        self.assertIn("facts_fts indexes 0 of 3 rows", check.detail)


class AtomicBuildTests(_Fixture):
    def test_an_interrupted_build_leaves_the_index_as_it_was(self):
        drop_then_fail = (
            "DROP TRIGGER IF EXISTS facts_ai; DROP TRIGGER IF EXISTS facts_ad; DROP TRIGGER IF EXISTS facts_au;"
            "DROP TABLE IF EXISTS facts_fts;" + _FTS_SCHEMA + "; SELECT no_such_function()"
        )
        with self.assertRaises(sqlite3.OperationalError):
            _fts_built(self.store.db, drop_then_fail, "facts_fts")
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.fts_coverage()["facts_fts"], (3, 3))  # rolled back: not dropped, not emptied
        self.assertTrue(_fts_ok(self.store.db, "facts_fts"))

    def test_a_completed_build_is_whole(self):
        _fts_built(
            self.store.db,
            "DROP TRIGGER IF EXISTS facts_ai; DROP TRIGGER IF EXISTS facts_ad; DROP TRIGGER IF EXISTS facts_au;"
            "DROP TABLE IF EXISTS facts_fts;" + _FTS_SCHEMA,
            "facts_fts",
        )
        self.assertEqual(self.store.fts_coverage()["facts_fts"], (3, 3))
        self.assertTrue(_fts_ok(self.store.db, "facts_fts"))


if __name__ == "__main__":
    unittest.main()
