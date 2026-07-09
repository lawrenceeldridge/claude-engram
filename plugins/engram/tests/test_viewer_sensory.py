"""Phase 5 — the viewer surface for the sensory register + the snapshots index filter + the
doctor line. Asserts the PAGE HTML/JS (tab position, kind filter, render/dispatch wiring),
Store.sensory_stats, and that `engram doctor` reports the register. Stdlib unittest, no network.

(The PAGE's JS is separately syntax-checked by test_viewer.PageScriptTests via `node --check`.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.store import Store  # noqa: E402
from viewer.serve import PAGE  # noqa: E402


class SensoryTabPageTests(unittest.TestCase):
    def test_sensory_tab_is_leftmost(self):
        self.assertIn('data-view="sensory"', PAGE)
        # far-left: the sensory button precedes stm in the tab bar (right of the title)
        self.assertLess(PAGE.index('data-view="sensory"'), PAGE.index('data-view="stm"'))

    def test_snapshots_kind_filter_present(self):
        self.assertIn('<option value="snapshot">snapshots</option>', PAGE)

    def test_sensory_render_and_dispatch_wired(self):
        self.assertIn("async function reloadSensory()", PAGE)
        self.assertIn("view === 'sensory'", PAGE)
        self.assertIn("/api/sensory", PAGE)


class SensoryStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_stats_count_live_attended_and_modality(self):
        a = self.store.add_sensory("p", "visual", "t1", url="u1", now=1.0)
        self.store.add_sensory("p", "visual", "t2", url="u2", now=2.0)
        self.store.add_sensory("p", "verbal", "conv", now=3.0)
        self.store.mark_attended(a)
        self.assertEqual(self.store.sensory_stats("p"), {"live": 3, "attended": 1, "visual": 2, "verbal": 1})

    def test_stats_excludes_decayed(self):
        self.store.add_sensory("p", "visual", "t", url="u", now=1.0)
        self.store.sweep_sensory("p", capacity=0, ttl_seconds=1, now=1000.0)  # tombstones it (decayed_at set)
        self.assertEqual(self.store.sensory_stats("p"), {"live": 0, "attended": 0, "visual": 0, "verbal": 0})


class DoctorSensoryTests(unittest.TestCase):
    def test_doctor_prints_the_sensory_line(self):
        with tempfile.TemporaryDirectory() as d:
            env = {k: v for k, v in os.environ.items() if k != "ENGRAM_DISABLE"}
            env.update({"ENGRAM_DATA_DIR": d, "ENGRAM_EMBEDDING": "hash"})
            r = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "engram"), "doctor"],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("sensory", r.stdout)


if __name__ == "__main__":
    unittest.main()
