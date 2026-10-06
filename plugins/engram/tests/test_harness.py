"""The test harness itself (tests/_harness.py): the LLM / live-store guards, the hermetic env,
the env helpers — and the meta-test that every test module installs the harness."""

from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import _harness
from _harness import ROOT, LiveStoreInTest, LLMCallInTest, allow_llm_transport, scoped_env, temp_data_dir


class LLMGuardTests(unittest.TestCase):
    def test_claude_spawn_is_refused_however_it_is_named(self):
        for call in (
            lambda: subprocess.run(["claude", "-p", "x"]),
            lambda: subprocess.run(["/opt/homebrew/bin/claude", "-p"]),
            lambda: subprocess.run("claude -p x", shell=True),
            lambda: subprocess.Popen(["anything"], executable="/usr/local/bin/claude"),
        ):
            with self.assertRaises(LLMCallInTest):
                call()

    def test_any_http_request_is_refused(self):
        with self.assertRaises(LLMCallInTest):
            urllib.request.urlopen("http://127.0.0.1:11434/v1/chat/completions")
        with self.assertRaises(LLMCallInTest):
            urllib.request.urlopen(urllib.request.Request("http://localhost:1/x", method="POST"))

    def test_the_error_escapes_a_fail_open_except_exception(self):
        def fail_open_distiller():
            try:
                subprocess.run(["claude", "-p", "distil this"])
            except Exception:  # the distillers' fallback — must NOT swallow the guard
                return "heuristic fallback"

        with self.assertRaises(LLMCallInTest):
            fail_open_distiller()

    def test_ordinary_children_still_run(self):
        out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "ok")

    def test_allow_llm_transport_opts_one_block_in_and_nests(self):
        with allow_llm_transport():
            with allow_llm_transport():
                pass
            with self.assertRaises(urllib.error.URLError):  # reached the network layer: nothing on port 1
                urllib.request.urlopen("http://127.0.0.1:1", timeout=0.5)
        with self.assertRaises(LLMCallInTest):  # and the guard is back on afterwards
            urllib.request.urlopen("http://127.0.0.1:1", timeout=0.5)

    def test_program_name_resolution(self):
        cases = [
            ((["claude", "-p"], None, False), "claude"),
            (("/usr/bin/Claude.exe", None, False), "claude"),
            (("claude -p x", None, True), "claude"),
            ((["python3", "claude"], None, False), "python3"),
            ((["x"], "/bin/claude", False), "claude"),
            (([], None, False), ""),
        ]
        for args, expected in cases:
            self.assertEqual(_harness._program(*args), expected, args)


class HermeticEnvTests(unittest.TestCase):
    def test_only_the_harness_settings_remain(self):
        engram = {k for k in os.environ if k.startswith(("ENGRAM_", "CLAUDE_PLUGIN_"))}
        self.assertEqual(engram, {"ENGRAM_DATA_DIR", "ENGRAM_DISTILLER"})  # a leaked test env shows up here
        self.assertEqual(os.environ["ENGRAM_DISTILLER"], "heuristic")
        self.assertNotIn("CLAUDE_PROJECT_DIR", os.environ)
        self.assertNotIn("CLAUDE_MEM_DATA_DIR", os.environ)

    def test_the_data_dir_is_a_temp_dir_outside_the_real_store(self):
        data = Path(os.environ["ENGRAM_DATA_DIR"]).resolve()
        self.assertTrue(data.is_dir())
        self.assertFalse(_harness._is_protected(data))

    def test_children_inherit_the_offline_settings(self):
        code = "import os; print(os.environ['ENGRAM_DISTILLER'], os.environ['ENGRAM_DATA_DIR'])"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split(), ["heuristic", os.environ["ENGRAM_DATA_DIR"]])

    def test_install_is_idempotent(self):
        connect, popen_init, urlopen = sqlite3.connect, subprocess.Popen.__init__, urllib.request.urlopen
        _harness._install()
        self.assertIs(sqlite3.connect, connect)
        self.assertIs(subprocess.Popen.__init__, popen_init)
        self.assertIs(urllib.request.urlopen, urlopen)


class LiveStoreGuardTests(unittest.TestCase):
    def test_the_real_plugin_data_dir_is_protected(self):
        self.assertTrue(_harness._is_protected((Path.home() / ".claude" / "plugins" / "data").resolve()))
        probe = Path.home() / ".claude" / "plugins" / "data" / "engram-harness-probe" / "never.db"
        with self.assertRaises(LiveStoreInTest):  # refused before sqlite touches the filesystem
            sqlite3.connect(probe)

    def test_read_write_is_refused_and_read_only_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "memory.db"
            sqlite3.connect(db).close()  # created before the root is protected
            with mock.patch.object(_harness, "_protected", (Path(root).resolve(),)):
                for call in (
                    lambda: sqlite3.connect(db),
                    lambda: sqlite3.connect(str(db), timeout=1.0),
                    lambda: sqlite3.connect(f"file:{db}", uri=True),
                    lambda: sqlite3.connect(f"file:{db}?mode=rw", uri=True),
                ):
                    with self.assertRaises(LiveStoreInTest):
                        call()
                ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # config._has_memories' probe
                self.assertEqual(ro.execute("SELECT 1").fetchone(), (1,))
                ro.close()
                sqlite3.connect(":memory:").close()

    def test_other_paths_are_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            sqlite3.connect(Path(root) / "scratch.db").close()


class EnvHelperTests(unittest.TestCase):
    def test_scoped_env_sets_unsets_and_restores(self):
        with mock.patch.dict(os.environ, {"ENGRAM_HARNESS_A": "before"}):
            case = unittest.TestCase()
            scoped_env(case, ENGRAM_HARNESS_A=None, ENGRAM_HARNESS_B="new")
            self.assertNotIn("ENGRAM_HARNESS_A", os.environ)
            self.assertEqual(os.environ["ENGRAM_HARNESS_B"], "new")
            os.environ["ENGRAM_HARNESS_C"] = "set inside the test"
            case.doCleanups()
            self.assertEqual(os.environ["ENGRAM_HARNESS_A"], "before")
            self.assertNotIn("ENGRAM_HARNESS_B", os.environ)
            self.assertNotIn("ENGRAM_HARNESS_C", os.environ)

    def test_temp_data_dir_exports_then_removes(self):
        before = os.environ["ENGRAM_DATA_DIR"]
        case = unittest.TestCase()
        tmp = temp_data_dir(case)
        self.assertEqual(os.environ["ENGRAM_DATA_DIR"], tmp.name)
        self.assertTrue(Path(tmp.name).is_dir())
        case.doCleanups()
        self.assertEqual(os.environ["ENGRAM_DATA_DIR"], before)
        self.assertFalse(Path(tmp.name).exists())


class EveryModuleInstallsTheHarnessTests(unittest.TestCase):
    """A new test file that forgets the harness would run unguarded — fail it here instead."""

    def test_every_test_module_imports_the_harness_before_the_code_under_test(self):
        modules = sorted((ROOT / "tests").glob("test_*.py"))
        self.assertGreater(len(modules), 40)
        for path in modules:
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
                names = [n.module if isinstance(n, ast.ImportFrom) else n.names[0].name for n in imports]
                self.assertIn("_harness", names, "add `import _harness  # noqa: F401` (or `from _harness import …`)")
                first_harness = names.index("_harness")
                local = [i for i, name in enumerate(names) if name and name.split(".")[0] == "core"]
                self.assertTrue(all(first_harness < i for i in local), "import _harness before `core`")

    def test_no_module_edits_sys_path_itself(self):
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            with self.subTest(module=path.name):
                calls = [
                    node
                    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                    if isinstance(node, ast.Call) and ast.unparse(node.func) == "sys.path.insert"
                ]
                self.assertEqual(calls, [], "the harness owns import paths")


if __name__ == "__main__":
    unittest.main()
