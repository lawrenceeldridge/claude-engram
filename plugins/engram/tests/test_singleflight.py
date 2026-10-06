"""Single-flight locks (core/singleflight.py) and the bin/ workers that hold one."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import temp_data_dir

import index_docs
import index_edit

from core import singleflight
from core.config import get_config
from core.store import Store


def _dead_pid() -> int:
    """A pid that has exited (a finished child)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class SingleflightTests(unittest.TestCase):
    def setUp(self):
        self.lock = Path(temp_data_dir(self).name) / ".job.lock"

    def test_one_holder_at_a_time_then_free(self):
        self.assertTrue(singleflight.acquire(self.lock))
        self.assertFalse(singleflight.acquire(self.lock))  # held by a live process (this one)
        singleflight.release(self.lock)
        self.assertFalse(self.lock.exists())
        self.assertTrue(singleflight.acquire(self.lock))
        singleflight.release(self.lock)

    def test_a_dead_holders_lock_is_reclaimed(self):
        self.lock.write_text(str(_dead_pid()))
        self.assertTrue(singleflight.acquire(self.lock))
        self.assertEqual(self.lock.read_text(), str(os.getpid()))
        singleflight.release(self.lock)

    def test_release_never_drops_another_processes_lock(self):
        self.lock.write_text("1")  # pid 1 is always alive and never us
        singleflight.release(self.lock)
        self.assertTrue(self.lock.exists())

    def test_held_releases_on_exit_and_on_error(self):
        with singleflight.held(self.lock) as mine:
            self.assertTrue(mine)
            with singleflight.held(self.lock) as second:
                self.assertFalse(second)  # a second taker gets False and leaves it alone
            self.assertTrue(self.lock.exists())
        self.assertFalse(self.lock.exists())
        with self.assertRaises(RuntimeError):
            with singleflight.held(self.lock):
                raise RuntimeError("job failed")
        self.assertFalse(self.lock.exists())

    def test_holder_reports_a_live_holder_and_its_age_only(self):
        self.assertIsNone(singleflight.holder(self.lock))
        singleflight.acquire(self.lock)
        pid, age = singleflight.holder(self.lock, now=self.lock.stat().st_mtime + 90)
        self.assertEqual((pid, round(age)), (os.getpid(), 90))
        singleflight.release(self.lock)
        self.lock.write_text(str(_dead_pid()))
        self.assertIsNone(singleflight.holder(self.lock))  # a dead holder is no holder


class IndexWorkerTests(unittest.TestCase):
    """bin/index_docs and bin/index_edit index under their lock, and yield to a live holder."""

    def setUp(self):
        self.data = Path(temp_data_dir(self).name)
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        self.repo = Path(repo.name)
        (self.repo / ".git").mkdir()
        self.file = self.repo / "mod.py"
        self.file.write_text("def deploy():\n    return 1\n", encoding="utf-8")

    def _chunks(self) -> int:
        store = Store(get_config().db_path)
        try:
            return store.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        finally:
            store.close()

    def _index_docs(self) -> None:
        fd, payload = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"cwd": str(self.repo)}, fh)
        index_docs._run_worker(payload)

    def test_index_docs_indexes_and_releases_its_lock(self):
        self._index_docs()
        self.assertGreater(self._chunks(), 0)
        self.assertFalse((self.data / ".index.lock").exists())

    def test_index_docs_yields_to_a_live_holder(self):
        (self.data / ".index.lock").write_text("1")
        self._index_docs()
        self.assertEqual(self._chunks(), 0)

    def test_index_edit_drains_the_dirty_list(self):
        dirty, lock = index_edit._paths(self.data)
        dirty.write_text(f"{self.file}\n{self.file}\n", encoding="utf-8")
        index_edit._run_worker()
        self.assertGreater(self._chunks(), 0)
        self.assertFalse(dirty.exists())
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
