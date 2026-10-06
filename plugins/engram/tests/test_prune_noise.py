"""`engram prune-noise` — the reversible retro-sweep (capture-quality-gate Phase 4).

Drives the CLI end-to-end as a subprocess (argparse + cmd_prune_noise), seeding the store
directly. Asserts: dry-run reports but writes nothing; --yes archives exactly the low-value
rows (reversible status 'pruned') and keeps real facts (even short ones); prompt rows are
judged by the prompt gate; the sweep is idempotent. Stdlib unittest, hash embedder, no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _harness import ROOT

from core.store import Store

# (id, kind, text, is_noise) — the seeded population. is_noise = should be archived by the sweep.
_SEED = [
    ("f1", "discovery", "TypeScript compilation passed", True),  # ephemeral status
    ("f2", "discovery", "<task-notification>agent finished</task-notification>", True),  # harness
    ("f3", "prompt", "Option C", True),  # trivial prompt (confirmation)
    ("f4", "prompt", "yes lets commit", True),  # trivial prompt (directive) — prompt gate only
    ("f5", "discovery", "The auth module uses JWT rotation with a 15m TTL.", False),  # real fact
    ("f6", "discovery", "Use int8 vectors", False),  # short but REAL — must survive
]
_NOISE_IDS = {fid for fid, _k, _t, noise in _SEED if noise}
_KEEP_IDS = {fid for fid, _k, _t, noise in _SEED if not noise}


class PruneNoiseTests(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.db_path = Path(self.data.name) / "memory.db"
        self._seed()

    def tearDown(self):
        self.data.cleanup()

    def _seed(self):
        store = Store(self.db_path)
        for fid, kind, text, _noise in _SEED:
            store.db.execute(
                "INSERT INTO facts (id, project_key, project_label, project_path, session_id, kind, "
                "text, observation_id, created_at, status, tier) "
                "VALUES (?, 'pk', 'proj', '/proj', 's', ?, ?, NULL, 1.0, 'active', 'stm')",
                (fid, kind, text),
            )
        store.db.commit()
        store.close()

    def _run(self, *args):
        env = {
            **os.environ,
            "ENGRAM_DATA_DIR": self.data.name,
        }
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "engram"), "prune-noise", *args],
            text=True,
            capture_output=True,
            env=env,
        )

    def _status(self) -> dict[str, str]:
        store = Store(self.db_path)
        rows = store.db.execute("SELECT id, status FROM facts").fetchall()
        store.close()
        return {r["id"]: r["status"] for r in rows}

    def test_dry_run_reports_without_writing(self):
        r = self._run("--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("4 low-value memories", r.stdout)
        self.assertIn("dry-run", r.stdout)
        self.assertTrue(all(s == "active" for s in self._status().values()))  # nothing archived

    def test_yes_archives_only_noise_reversibly(self):
        r = self._run("--all", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("archived 4", r.stdout)
        self.assertIn("'pruned'", r.stdout)
        status = self._status()
        self.assertTrue(all(status[fid] == "pruned" for fid in _NOISE_IDS))
        self.assertTrue(all(status[fid] == "active" for fid in _KEEP_IDS))  # real facts untouched

    def test_short_real_fact_survives(self):
        self._run("--all", "--yes")
        self.assertEqual(self._status()["f6"], "active")  # "Use int8 vectors" kept

    def test_sweep_is_idempotent(self):
        self._run("--all", "--yes")
        r2 = self._run("--all", "--yes")
        self.assertIn("no low-value memories found", r2.stdout)  # nothing active left to archive

    def test_unknown_project_is_clean_noop(self):
        r = self._run("--project", "does-not-exist")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no project matching", r.stdout)
        self.assertTrue(all(s == "active" for s in self._status().values()))


if __name__ == "__main__":
    unittest.main()
