"""Hook-layer behaviour: throttled Stop summary, SessionStart orientation, PreToolUse guard.

The service functions are unit-tested with a stub distiller (no live LLM); the PreToolUse
guard is exercised as a subprocess (it's a stdin/stdout hook). Sessions are namespaced by
PID so the per-session dedupe markers don't collide across runs.

Run: python3 -m unittest discover -s plugins/engram/tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports.distill import DistilledFact  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402


class _StubSummarizer:
    def summarize(self, text):
        return DistilledFact(
            text="session did things", title="Session", narrative="Investigated: x", type="session_summary"
        )

    def distill(self, text, existing):
        return []


class SummaryThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}
        self.tx = Path(self.tmp.name) / "t.jsonl"

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _write(self, nbytes: int) -> None:
        msg = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * nbytes}]},
        }
        self.tx.write_text(json.dumps(msg) + "\n", encoding="utf-8")

    def _call(self, **kw) -> int:
        with mock.patch.object(service, "get_distiller", return_value=_StubSummarizer()):
            return service.maybe_capture_summary(
                self.store, self.embedder, self.cfg, self.project, "s1", str(self.tx), **kw
            )

    def test_below_threshold_does_not_summarise(self):
        self._write(100)
        self.assertEqual(self._call(force=False), 0)
        self.assertIsNone(self.store.latest_summary("p"))

    def test_force_summarises_regardless(self):
        self._write(100)
        self.assertEqual(self._call(force=True), 1)
        self.assertIsNotNone(self.store.latest_summary("p"))

    def test_throttle_suppresses_until_growth(self):
        self._write(100)
        self._call(force=True)  # cursor now at ~file size
        self.assertEqual(self._call(force=False), 0)  # no growth
        self._write(20000)  # grow well past the default 8000-byte threshold
        self.assertEqual(self._call(force=False), 1)


class OrientationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "acme"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_empty_when_no_summary(self):
        self.assertEqual(service.orientation_block(self.store, self.project), "")

    def test_renders_latest_summary(self):
        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            [
                DistilledFact(
                    text="did things",
                    title="Refactor X",
                    narrative="Investigated: a\nCompleted: b",
                    type="session_summary",
                )
            ],
            kind="session_summary",
        )
        block = service.orientation_block(self.store, self.project)
        self.assertIn("acme", block)
        self.assertIn("Refactor X", block)
        self.assertIn("Completed: b", block)


class PreToolUseGuardTests(unittest.TestCase):
    """Subprocess tests of bin/prefer_memory.py."""

    def setUp(self):
        self.sess = f"test-{os.getpid()}"
        self.markers = []

    def tearDown(self):
        for tag in ("prefer", "readguard", "consulted"):
            (Path(tempfile.gettempdir()) / f"engram-{tag}-{self.sess}.seen").unlink(missing_ok=True)

    def _consulted_marker(self) -> Path:
        return Path(tempfile.gettempdir()) / f"engram-consulted-{self.sess}.seen"

    def _mark_consulted(self, tool: str) -> None:
        subprocess.run(
            [sys.executable, str(ROOT / "bin" / "mark_consulted.py")],
            input=json.dumps({"tool_name": tool, "session_id": self.sess}),
            text=True,
            capture_output=True,
        )

    def _run(self, payload: dict, enforce: str = "advisory") -> str:
        env = {**os.environ, "ENGRAM_ENFORCE": enforce}
        payload.setdefault("session_id", self.sess)
        r = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "prefer_memory.py")],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
        )
        return r.stdout.strip()

    def _big_py(self) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write("x = 1\n" * 2000)  # ~12KB
        f.close()
        return f.name

    def test_grep_reminder_once_then_silent(self):
        first = self._run({"tool_name": "Grep", "tool_input": {"pattern": "foo"}})
        self.assertIn("search_code", first)
        second = self._run({"tool_name": "Glob", "tool_input": {"pattern": "*.py"}})
        self.assertEqual(second, "")

    def test_enforce_off_is_silent(self):
        self.assertEqual(self._run({"tool_name": "Grep", "tool_input": {"pattern": "foo"}}, enforce="off"), "")

    def _bash(self, command: str) -> str:
        # isolate the data dir so the destructive-uninstall guard is tested independently of
        # whatever anti-patterns happen to be in the real store
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "ENGRAM_ENFORCE": "advisory", "ENGRAM_DATA_DIR": tmp}
            r = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "prefer_memory.py")],
                input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "session_id": self.sess}),
                text=True,
                capture_output=True,
                env=env,
            )
            return r.stdout.strip()

    def test_raw_uninstall_warns(self):
        out = self._bash("claude plugin uninstall engram@claude-engram")
        self.assertIn("STOP", out)
        self.assertIn("engram uninstall", out)  # points at the safe wrapper

    def test_uninstall_with_keep_data_is_silent(self):
        self.assertEqual(self._bash("claude plugin uninstall engram@claude-engram --keep-data"), "")

    def test_engram_uninstall_wrapper_is_silent(self):
        # our own safe command must not trip its own guard
        self.assertEqual(self._bash("engram uninstall --purge-data"), "")

    def test_other_plugin_uninstall_is_silent(self):
        self.assertEqual(self._bash("claude plugin uninstall cost-guard@cost-guard"), "")

    def test_large_code_read_advises(self):
        out = self._run({"tool_name": "Read", "tool_input": {"file_path": self._big_py()}})
        self.assertIn("get_symbol", out)

    def test_read_with_offset_is_silent(self):
        out = self._run({"tool_name": "Read", "tool_input": {"file_path": self._big_py(), "offset": 1, "limit": 20}})
        self.assertEqual(out, "")

    def test_small_read_is_silent(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
        f.write("x = 1\n")
        f.close()
        self.assertEqual(self._run({"tool_name": "Read", "tool_input": {"file_path": f.name}}), "")

    def test_non_code_read_is_silent(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
        f.write("# doc\n" * 2000)
        f.close()
        self.assertEqual(self._run({"tool_name": "Read", "tool_input": {"file_path": f.name}}), "")

    def test_strict_denies_grep_until_memory_consulted(self):
        self._consulted_marker().unlink(missing_ok=True)
        denied = self._run({"tool_name": "Grep", "tool_input": {"pattern": "x"}}, enforce="strict")
        self.assertIn('"permissionDecision": "deny"', denied)
        self._mark_consulted("mcp__plugin_engram_engram-memory__recall")  # now memory has been consulted
        self.assertTrue(self._consulted_marker().exists())
        allowed = self._run({"tool_name": "Grep", "tool_input": {"pattern": "x"}}, enforce="strict")
        self.assertEqual(allowed, "")

    def test_advisory_reminds_grep_when_not_consulted(self):
        self._consulted_marker().unlink(missing_ok=True)
        out = self._run({"tool_name": "Grep", "tool_input": {"pattern": "x"}})
        self.assertIn("recall", out)

    def test_consulted_grep_is_silent(self):
        self._mark_consulted("mcp__plugin_engram_engram-memory__search_code")
        self.assertEqual(self._run({"tool_name": "Grep", "tool_input": {"pattern": "x"}}), "")

    def test_mark_consulted_skips_index_docs(self):
        self._consulted_marker().unlink(missing_ok=True)
        self._mark_consulted("mcp__plugin_engram_engram-memory__index_docs")
        self.assertFalse(self._consulted_marker().exists())

    # --- Bash search is gated like Grep/Glob (closes the Bash bypass) ---

    def test_bash_search_reminds_when_not_consulted(self):
        self._consulted_marker().unlink(missing_ok=True)
        out = self._run({"tool_name": "Bash", "tool_input": {"command": "rg 'def foo' ."}})
        self.assertIn("recall", out)

    def test_bash_strict_denies_recursive_grep(self):
        self._consulted_marker().unlink(missing_ok=True)
        denied = self._run({"tool_name": "Bash", "tool_input": {"command": "grep -r foo src/"}}, enforce="strict")
        self.assertIn('"permissionDecision": "deny"', denied)

    def test_bash_pipe_filter_is_silent(self):
        # `ps aux | grep` is a stdin filter, not a codebase search — must never be gated.
        self._consulted_marker().unlink(missing_ok=True)
        out = self._run({"tool_name": "Bash", "tool_input": {"command": "ps aux | grep python"}}, enforce="strict")
        self.assertEqual(out, "")

    def test_bash_non_search_is_silent(self):
        self._consulted_marker().unlink(missing_ok=True)
        out = self._run({"tool_name": "Bash", "tool_input": {"command": "ls -la && npm test"}}, enforce="strict")
        self.assertEqual(out, "")

    def test_strict_denies_read_of_indexed_code(self):
        from core.index.indexer import index_file
        from core.ports.embedding import HashEmbedding
        from core.project import resolve_project

        data, repo = tempfile.mkdtemp(), tempfile.mkdtemp()
        Path(repo, ".git").touch()  # marker so resolve_project keys consistently
        fp = Path(repo) / "big.py"
        fp.write_text("def f():\n    return 1\n" + "# pad line\n" * 2000, encoding="utf-8")
        os.environ["ENGRAM_DATA_DIR"] = data
        try:
            cfg = get_config()
            store = Store(cfg.db_path)
            index_file(store, HashEmbedding(dim=cfg.dim), cfg, resolve_project(repo, cfg.markers), str(fp))
            store.close()
            out = self._run({"tool_name": "Read", "tool_input": {"file_path": str(fp)}}, enforce="strict")
            self.assertIn('"permissionDecision": "deny"', out)
        finally:
            os.environ.pop("ENGRAM_DATA_DIR", None)


class UninstallCommandTests(unittest.TestCase):
    """`engram uninstall` keeps memory by default; --purge-data is the explicit opt-in."""

    def _dry(self, *flags: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "ENGRAM_DATA_DIR": tmp, "ENGRAM_REEXECED": "1"}
            r = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "engram"), "uninstall", "--dry-run", *flags],
                text=True,
                capture_output=True,
                env=env,
            )
            return r.stdout

    def test_default_keeps_data_and_passes_keep_data(self):
        out = self._dry()
        self.assertIn("--keep-data", out)  # the harness flag that prevents deletion
        self.assertIn("KEEP", out)

    def test_purge_data_is_explicit_opt_in(self):
        self.assertIn("PURGE", self._dry("--purge-data"))


if __name__ == "__main__":
    unittest.main()
