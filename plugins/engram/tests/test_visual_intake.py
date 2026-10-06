"""Visual intake path (Phase 3b) — the attention gate (record_visual_perception), promotion into
the index (promote_visual_perceptions), and the PostToolUse intake hook. Stdlib unittest, hash
embedder, no network.

Attention here is A-S *re-perception* (returning to the same page within the window), never
rehearsal. Promotion runs off the hot path; the hook is cheap + fail-open with no embedding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import index_snapshot as hook
from _harness import ROOT

from core.config import get_config
from core.domain.sensory import normalize_url
from core.ports.embedding import HashEmbedding
from core.service import promote_visual_perceptions, record_visual_perception
from core.store import Store

_A11Y = 'heading "Login"\ntextbox "Email"\nbutton "Sign in"'


class NormalizeUrlTests(unittest.TestCase):
    def test_strips_query_fragment_slash_and_lowercases(self):
        self.assertEqual(normalize_url("https://X/App/"), "https://x/app")
        self.assertEqual(normalize_url("https://x/app?tab=1#top"), "https://x/app")
        self.assertEqual(normalize_url("https://x/app"), normalize_url("https://x/app/"))

    def test_empty(self):
        self.assertEqual(normalize_url(""), "")
        self.assertEqual(normalize_url(None), "")


class RecordVisualPerceptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = replace(get_config(), attention_window_seconds=300)
        self.store = Store(Path(self.tmp.name) / "memory.db")
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _rec(self, url, text, now):
        return record_visual_perception(self.store, self.cfg, self.project, url, text, now)

    def test_first_perception_not_attended(self):
        self.assertFalse(self._rec("https://x/app", _A11Y, 1000.0)["attended"])
        rows = self.store.sensory_rows("proj")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attended"], 0)

    def test_reperception_same_url_new_content_attends_both(self):
        self._rec("https://x/app", _A11Y, 1000.0)
        self.assertTrue(self._rec("https://x/app", 'heading "Dashboard"', 1050.0)["attended"])
        self.assertTrue(all(r["attended"] == 1 for r in self.store.sensory_rows("proj")))

    def test_reperception_identical_content_attends_no_duplicate(self):
        self._rec("https://x/app", _A11Y, 1000.0)
        self.assertTrue(self._rec("https://x/app", _A11Y, 1050.0)["attended"])  # content re-perception
        rows = self.store.sensory_rows("proj")
        self.assertEqual(len(rows), 1)  # upsert, no dup
        self.assertEqual(rows[0]["attended"], 1)

    def test_url_normalization_matches_reperception(self):
        self._rec("https://x/app", _A11Y, 1000.0)
        self.assertTrue(self._rec("https://x/app/?tab=2", 'heading "Other"', 1050.0)["attended"])

    def test_outside_window_not_attended(self):
        self._rec("https://x/app", _A11Y, 1000.0)
        self.assertFalse(self._rec("https://x/app", 'heading "Later"', 1000.0 + 10_000)["attended"])

    def test_different_pages_not_attended(self):
        self._rec("https://x/a", _A11Y, 1000.0)
        self.assertFalse(self._rec("https://x/b", _A11Y, 1001.0)["attended"])


class PromoteVisualTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = replace(get_config(), embedding="hash", attention_window_seconds=300)
        self.store = Store(Path(self.tmp.name) / "memory.db")
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _rec(self, url, text, now):
        return record_visual_perception(self.store, self.cfg, self.project, url, text, now)

    def test_promotes_attended_into_index_and_leaves_register(self):
        self._rec("https://x/app", _A11Y, 1000.0)
        self._rec("https://x/app", 'heading "Dashboard"', 1050.0)  # re-perception -> attended
        n = promote_visual_perceptions(self.store, self.embedder, self.cfg, self.project, 1100.0)
        self.assertGreaterEqual(n, 1)
        self.assertTrue(self.store.chunk_rows("proj", kind="snapshot"))  # in the index
        self.assertEqual(self.store.sensory_rows("proj"), [])  # promoted rows left the live register

    def test_unattended_not_promoted(self):
        self._rec("https://x/app", _A11Y, 1000.0)  # single perception -> not attended
        n = promote_visual_perceptions(self.store, self.embedder, self.cfg, self.project, 1100.0)
        self.assertEqual(n, 0)
        self.assertEqual(self.store.chunk_rows("proj", kind="snapshot"), [])
        self.assertEqual(len(self.store.sensory_rows("proj")), 1)  # still live

    def test_recall_isolation_promotion_never_writes_facts(self):
        self._rec("https://x/app", _A11Y, 1000.0)
        self._rec("https://x/app", 'heading "Dashboard"', 1050.0)
        promote_visual_perceptions(self.store, self.embedder, self.cfg, self.project, 1100.0)
        self.assertEqual(self.store.active_count("proj"), 0)  # visual -> index, never the facts surface


class HookHelperTests(unittest.TestCase):
    def test_result_text_shapes(self):
        self.assertEqual(hook._result_text("plain"), "plain")
        self.assertEqual(
            hook._result_text({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}), "a\nb"
        )
        self.assertEqual(hook._result_text([{"type": "text", "text": "x"}]), "x")
        self.assertEqual(hook._result_text({"result": "r"}), "r")
        self.assertEqual(hook._result_text(None), "")
        self.assertEqual(hook._result_text(123), "")  # unknown shape -> no-op

    def test_extract_url(self):
        self.assertEqual(hook._extract_url('- Page URL: https://x/app\nheading "h"'), "https://x/app")
        self.assertEqual(hook._extract_url("blah https://y/z?q=1 more"), "https://y/z?q=1")
        self.assertIsNone(hook._extract_url("no url here"))


class HookSubprocessTests(unittest.TestCase):
    """The hook run as a real subprocess (as Claude Code invokes it), with a clean env."""

    def _run(self, payload, data_dir):
        env = {**os.environ, "ENGRAM_DATA_DIR": data_dir, "ENGRAM_SENSORY_ENABLED": "true"}
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "index_snapshot.py")],
            input=json.dumps(payload) if payload is not None else "not json",
            text=True,
            capture_output=True,
            cwd=str(ROOT),
            env=env,
        )

    def test_fail_open_on_bad_stdin(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._run(None, d).returncode, 0)

    def test_filepath_mode_skips(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run({"tool_input": {"filePath": "/tmp/snap.txt"}, "tool_response": _A11Y}, d)
            self.assertEqual(r.returncode, 0)
            s = Store(Path(d) / "memory.db")
            try:
                self.assertEqual(s.db.execute("SELECT COUNT(*) FROM sensory").fetchone()[0], 0)
            finally:
                s.close()

    def test_registers_perception(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run(
                {
                    "tool_name": "mcp__playwright__browser_snapshot",
                    "tool_response": "- Page URL: https://x/app\n" + _A11Y,
                },
                d,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            s = Store(Path(d) / "memory.db")
            try:
                rows = s.db.execute("SELECT modality, url, text FROM sensory").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["modality"], "visual")
                self.assertEqual(rows[0]["url"], "https://x/app")
                self.assertIn("Sign in", rows[0]["text"])
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
