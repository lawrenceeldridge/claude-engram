"""The schema-migration ladder (``Store._migrate``): which steps run on open, and what they build.

A store stamped N has run steps 1…N, so an upgrade runs only the steps after its stamp — a schema
bump costs its own step, not a replay of the whole ladder (seconds at 10⁵ facts). Below the floor
(fresh, or the legacy FTS flag of 1) and past the head (a downgrade) every step replays, and an
interrupted ladder resumes where its stamp left it.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from _harness import temp_data_dir

import core.store as store_module
from core.store import _LADDER_FLOOR, _MIGRATIONS, _SCHEMA_VERSION, Store

STEP_NAMES = [step.__name__ for step in _MIGRATIONS]


class _LadderFixture(unittest.TestCase):
    def setUp(self):
        self.path = Path(temp_data_dir(self).name) / "memory.db"
        Store(self.path).close()  # a store the whole ladder built

    def _stamp(self, version: int) -> None:
        store = Store(self.path)
        store.db.execute(f"PRAGMA user_version = {version}")
        store.db.commit()
        store.close()

    def _open_recording(self, fail_at: str | None = None) -> list[str]:
        """Open the store and return the names of the steps its ladder ran (``fail_at`` raises there)."""
        ran: list[str] = []

        def recording(step):
            def run(db):
                ran.append(step.__name__)
                if step.__name__ == fail_at:
                    raise RuntimeError(f"interrupted at {fail_at}")
                step(db)

            return run

        with mock.patch.object(store_module, "_MIGRATIONS", [recording(step) for step in _MIGRATIONS]):
            Store(self.path).close()
        return ran

    def _version(self) -> int:
        store = Store(self.path)
        try:
            return store.db.execute("PRAGMA user_version").fetchone()[0]
        finally:
            store.close()


class LadderTests(_LadderFixture):
    def test_a_current_store_runs_no_step(self):
        self.assertEqual(self._open_recording(), [])

    def test_an_upgrade_runs_only_the_steps_after_its_stamp(self):
        for version in (_LADDER_FLOOR, 8, 16, _SCHEMA_VERSION - 1):
            with self.subTest(stamped=version):
                self._stamp(version)
                self.assertEqual(self._open_recording(), STEP_NAMES[version:])
                self.assertEqual(self._version(), _SCHEMA_VERSION)

    def test_below_the_floor_every_step_replays(self):
        for version in range(_LADDER_FLOOR):  # fresh, and the legacy FTS flag of 1
            with self.subTest(stamped=version):
                self._stamp(version)
                self.assertEqual(self._open_recording(), STEP_NAMES)
                self.assertEqual(self._version(), _SCHEMA_VERSION)

    def test_a_store_stamped_by_newer_code_replays_every_step(self):
        self._stamp(_SCHEMA_VERSION + 3)  # opened by an older build after a newer one migrated it
        self.assertEqual(self._open_recording(), STEP_NAMES)
        self.assertEqual(self._version(), _SCHEMA_VERSION)

    def test_an_interrupted_ladder_resumes_from_its_stamp(self):
        self._stamp(16)
        with self.assertRaises(RuntimeError):
            self._open_recording(fail_at=STEP_NAMES[18])
        # The stamp is written only after the whole ladder, so it never moved: the next open
        # re-runs every step after 16, and then the store is current.
        self.assertEqual(self._open_recording(), STEP_NAMES[16:])
        self.assertEqual(self._version(), _SCHEMA_VERSION)


class ChunkIndexTests(_LadderFixture):
    def _indexes(self) -> set[str]:
        store = Store(self.path)
        try:
            return {
                r[0]
                for r in store.db.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'chunks'")
            }
        finally:
            store.close()

    def test_a_fresh_store_has_every_chunk_lookup_index(self):
        self.assertTrue(
            {"idx_chunks_project", "idx_chunks_kind", "idx_chunks_anchor", "idx_chunks_source"} <= self._indexes()
        )

    def test_an_upgrade_from_the_previous_schema_converges(self):
        store = Store(self.path)  # what a store stamped one step back carries: no lookup indexes yet
        store.db.executescript("DROP INDEX idx_chunks_kind; DROP INDEX idx_chunks_anchor;")
        store.db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION - 1}")
        store.db.commit()
        store.close()
        self.assertEqual(self._open_recording(), ["_v21_chunk_lookup_indexes"])
        self.assertTrue({"idx_chunks_project", "idx_chunks_kind", "idx_chunks_anchor"} <= self._indexes())


if __name__ == "__main__":
    unittest.main()
