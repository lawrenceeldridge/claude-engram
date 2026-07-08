"""Subprocess tests of bin/prefer_snapshot.py — the prefer-a11y-snapshot PreToolUse nudge.

Driven as a subprocess (it's a stdin/stdout hook). Sessions are namespaced by PID so the
per-session dedupe marker doesn't collide across runs. Every path must exit 0 (fail-open).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PreferSnapshotHookTests(unittest.TestCase):
    def setUp(self):
        self.sess = f"test-snap-{os.getpid()}"

    def tearDown(self):
        (Path(tempfile.gettempdir()) / f"engram-prefer-snapshot-{self.sess}.seen").unlink(missing_ok=True)

    def _run(self, tool: str, mode: str = "advisory", session: str | None = None) -> tuple[int, str]:
        env = {**os.environ, "ENGRAM_PREFER_SNAPSHOT": mode}
        payload = {"tool_name": tool, "tool_input": {}, "session_id": session or self.sess}
        r = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "prefer_snapshot.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
        )
        return r.returncode, r.stdout.strip()

    def test_advisory_nudges_once_then_silent(self):
        code, out = self._run("mcp__chrome-devtools__take_screenshot")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("compact_page_view", doc["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("permissionDecision", doc["hookSpecificOutput"])  # advisory never blocks
        # Same session, second screenshot: silent (once-per-session dedupe).
        code2, out2 = self._run("mcp__playwright__browser_take_screenshot")
        self.assertEqual(code2, 0)
        self.assertEqual(out2, "")

    def test_strict_denies_screenshot(self):
        code, out = self._run("mcp__BrowserMCP__browser_screenshot", mode="strict")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("take_snapshot", doc["hookSpecificOutput"]["permissionDecisionReason"])

    def test_off_is_silent(self):
        code, out = self._run("mcp__chrome-devtools__take_screenshot", mode="off")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_snapshot_tool_is_not_nudged(self):
        # take_snapshot is exactly what we want the model to use — never nudge it.
        code, out = self._run("mcp__chrome-devtools__take_snapshot")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_non_screenshot_tool_passes_through(self):
        code, out = self._run("Read")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_fails_open_on_bad_stdin(self):
        r = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "prefer_snapshot.py")],
            input="not json",
            text=True,
            capture_output=True,
            env={**os.environ, "ENGRAM_PREFER_SNAPSHOT": "advisory"},
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
