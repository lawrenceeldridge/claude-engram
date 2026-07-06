"""Capture re-entrancy guard (rescue-queue-hardening Phase 1).

The `claude` distiller runs headless `claude -p` inside the capture worker; that nested
Claude session must not fire engram's hooks and capture the distiller prompt as a session
(the loop that stuffed the rescue queue with prompt-template payloads). Verifies:
  - `ENGRAM_DISABLE` gates the shared `hooks_disabled()` helper,
  - each hook entry point exits 0 (no side effect) when it is set,
  - `ClaudeCliDistiller` sets it in the headless subprocess env,
  - capture drops a transcript that is itself a distiller prompt (defensive backstop).
Stdlib unittest, hash embedder, no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from _bootstrap import hooks_disabled  # noqa: E402

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports import distill  # noqa: E402
from core.ports.distill import ClaudeCliDistiller, is_distiller_prompt  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402

_HOOKS = [
    "capture.py",
    "recall_session_start.py",
    "recall_prompt.py",
    "index_docs.py",
    "prefer_memory.py",
    "mark_consulted.py",
    "index_edit.py",
    "credit_read.py",
]


class HooksDisabledHelperTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("ENGRAM_DISABLE")
        os.environ.pop("ENGRAM_DISABLE", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ENGRAM_DISABLE", None)
        else:
            os.environ["ENGRAM_DISABLE"] = self._saved

    def test_off_by_default(self):
        self.assertFalse(hooks_disabled())

    def test_on_when_set(self):
        os.environ["ENGRAM_DISABLE"] = "1"
        self.assertTrue(hooks_disabled())


class HookNoOpTests(unittest.TestCase):
    """Every hook exits 0 and does nothing when ENGRAM_DISABLE=1 (driven as a subprocess)."""

    def test_all_hooks_noop_when_disabled(self):
        env = {**os.environ, "ENGRAM_DISABLE": "1", "ENGRAM_DATA_DIR": tempfile.mkdtemp()}
        env.pop("ENGRAM_PYTHON", None)
        for hook in _HOOKS:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "bin" / hook)],
                input="{}",  # a hook that reads stdin gets empty JSON; the guard fires first anyway
                text=True,
                capture_output=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, f"{hook} did not exit 0: {proc.stderr[:200]}")
            self.assertEqual(proc.stdout, "", f"{hook} emitted output while disabled: {proc.stdout[:200]}")


class DistillerEnvTests(unittest.TestCase):
    def test_claude_distiller_sets_disable_env(self):
        captured = {}

        class _Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def fake_run(args, **kwargs):
            captured["env"] = kwargs.get("env")
            return _Result()

        orig = distill.subprocess.run
        distill.subprocess.run = fake_run
        try:
            ClaudeCliDistiller(cmd="claude")._complete("some prompt")
        finally:
            distill.subprocess.run = orig
        self.assertIsNotNone(captured["env"], "distiller must pass an explicit env")
        self.assertEqual(captured["env"].get("ENGRAM_DISABLE"), "1")


class DistillerPromptBackstopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_is_distiller_prompt(self):
        self.assertTrue(is_distiller_prompt("You extract durable long-term memory from a coding assistant session."))
        self.assertTrue(is_distiller_prompt("Summarise this coding-assistant session as one durable memory."))
        self.assertTrue(is_distiller_prompt("You are consolidating long-term memory for a coding assistant."))
        self.assertFalse(is_distiller_prompt("The deploy target is AWS Lambda."))

    def test_capture_text_skips_a_distiller_prompt(self):
        n = service.capture_text(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            "You extract durable long-term memory from a coding assistant session.\n\nGroup related facts…",
        )
        self.assertEqual(n, 0)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 0)

    def test_capture_text_still_stores_a_real_delta(self):
        n = service.capture_text(
            self.store, self.embedder, self.cfg, self.project, "s1", "The deploy target is fly.io."
        )
        self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
