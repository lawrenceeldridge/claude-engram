"""Viewer service-health tests — the header's bus / embedding / distiller chips.

Stdlib unittest, no network: the all-stdlib config (inproc / hash / heuristic) must
report every subsystem healthy, and unreachable probes must fail open to 'warn'
(the configured backend degrades to its stdlib fallback, never a hard error).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from viewer.serve import _disambiguate_labels, _service_health, _tcp_ok  # noqa: E402


class DisambiguateLabelsTests(unittest.TestCase):
    def test_colliding_basenames_get_parent_prefix(self):
        items = [
            {"label": "backend", "path": "/x/sak-replicate/backend"},
            {"label": "backend", "path": "/x/sak-assistant/backend"},
            {"label": "claude-engram", "path": "/y/claude-engram"},
        ]
        got = {it["label"] for it in _disambiguate_labels(items)}
        self.assertEqual(got, {"sak-replicate/backend", "sak-assistant/backend", "claude-engram"})

    def test_unique_labels_untouched(self):
        items = [{"label": "a", "path": "/p/a"}, {"label": "b", "path": "/p/b"}]
        self.assertEqual([it["label"] for it in _disambiguate_labels(items)], ["a", "b"])


class PageScriptTests(unittest.TestCase):
    """The viewer page's inline <script> must be valid JS. Guards against Python string
    escaping (e.g. a bare `\\n` in the triple-quoted PAGE) silently corrupting the JS —
    which blanks the whole UI. Skipped when node isn't available."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_served_script_is_valid_js(self):
        from viewer.serve import PAGE

        m = re.search(r"<script>(.*)</script>", PAGE, re.S)
        self.assertIsNotNone(m, "no <script> block in PAGE")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(m.group(1))
            path = fh.name
        result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class TcpOkTests(unittest.TestCase):
    def test_closed_port_is_unreachable(self):
        # Port 1 is not listening — connection refused, fast.
        self.assertFalse(_tcp_ok("http://127.0.0.1:1", timeout=0.2))

    def test_garbage_url_is_unreachable(self):
        self.assertFalse(_tcp_ok("not-a-url", timeout=0.2))
        self.assertFalse(_tcp_ok("http://", timeout=0.2))  # no host


class ServiceHealthTests(unittest.TestCase):
    def _cfg(self, **kw):
        # Pin the stdlib backends regardless of ambient ENGRAM_* env, then override.
        base = replace(get_config(), bus="inproc", embedding="hash", distiller="heuristic")
        return replace(base, **kw)

    def test_stdlib_defaults_all_ok(self):
        h = _service_health(self._cfg())
        self.assertEqual(h["bus"]["backend"], "inproc")
        self.assertEqual(h["embedding"]["backend"], "hash")
        self.assertEqual(h["distiller"]["backend"], "heuristic")
        self.assertEqual({s["state"] for s in h.values()}, {"ok"})

    def test_nats_unreachable_warns_and_falls_open(self):
        h = _service_health(self._cfg(bus="nats", nats_url="nats://127.0.0.1:1"))
        self.assertEqual(h["bus"]["backend"], "nats")
        self.assertEqual(h["bus"]["state"], "warn")
        self.assertIn("inproc", h["bus"]["detail"])  # names the fallback

    def test_llm_distiller_unreachable_warns(self):
        h = _service_health(self._cfg(distiller="ollama", distiller_base_url="http://127.0.0.1:1"))
        self.assertTrue(h["distiller"]["backend"].startswith("ollama"))
        self.assertEqual(h["distiller"]["state"], "warn")
        self.assertIn("heuristic", h["distiller"]["detail"])  # names the fallback


if __name__ == "__main__":
    unittest.main()
