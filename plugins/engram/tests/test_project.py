"""Project-identity resolution — workspace-root default, marker mode, `.engram-root`.

Stdlib unittest, no network. Verifies the default `identity='workspace'` keys on the
folder the session was opened in (CLAUDE_PROJECT_DIR, else cwd) without walking to a
git/monorepo root; that `identity='marker'` restores the legacy walk-up; that
`.engram-root` overrides both; and that keys stay collision-free across same-named folders.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.project import resolve_project  # noqa: E402

MARKERS = (".git", "pyproject.toml", "package.json")


class ResolveProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, *rel):
        p = self.root.joinpath(*rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        return p

    def _mkdir(self, *rel):
        p = self.root.joinpath(*rel)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # --- workspace mode (default) -------------------------------------------------

    def test_workspace_keys_on_opened_folder_not_the_git_root(self):
        # Monorepo: .git at the root, a subfolder opened as the workspace (the moj-sak case).
        self._touch(".git")
        sub = self._mkdir("applications", "dune", "moj-sak")
        r = resolve_project(str(sub), MARKERS)  # default identity='workspace'
        self.assertEqual(r["path"], str(sub))  # the opened folder, NOT the monorepo root
        self.assertEqual(r["label"], "moj-sak")

    def test_workspace_prefers_project_dir_over_cwd(self):
        # CLAUDE_PROJECT_DIR is stable even when the terminal cwd is a nested subdir.
        ws = self._mkdir("moj-sak")
        src = self._mkdir("moj-sak", "src", "components")
        r = resolve_project(str(src), MARKERS, project_dir=str(ws))
        self.assertEqual(r["path"], str(ws))
        self.assertEqual(r["label"], "moj-sak")

    def test_workspace_does_not_fragment_into_nested_package(self):
        # Repo opened at its top; a nested package carries its own marker but must not win.
        self._touch(".git")
        self._touch("plugins", "engram", "pyproject.toml")
        r = resolve_project(str(self.root), MARKERS, project_dir=str(self.root))
        self.assertEqual(r["path"], str(self.root))
        self.assertEqual(r["label"], self.root.name)

    def test_workspace_falls_back_to_cwd_when_no_project_dir(self):
        sub = self._mkdir("x")
        r = resolve_project(str(sub), MARKERS)
        self.assertEqual(r["path"], str(sub))

    # --- marker mode (legacy, opt-in) ---------------------------------------------

    def test_marker_mode_walks_up_to_nearest_marker(self):
        self._touch(".git")
        sub = self._mkdir("apps", "svc")
        r = resolve_project(str(sub), MARKERS, identity="marker")
        self.assertEqual(r["path"], str(self.root))  # walked up to the .git root
        self.assertEqual(r["label"], self.root.name)

    def test_marker_mode_nested_marker_wins(self):
        self._touch(".git")
        self._touch("pkg", "pyproject.toml")
        pkg = self.root / "pkg"
        r = resolve_project(str(pkg), MARKERS, identity="marker")
        self.assertEqual(r["path"], str(pkg))  # nearest marker is the nested package
        self.assertEqual(r["label"], "pkg")

    def test_marker_mode_falls_back_to_start_when_no_marker(self):
        sub = self._mkdir("x")
        r = resolve_project(str(sub), MARKERS, identity="marker")
        self.assertEqual(r["path"], str(sub))

    # --- .engram-root overrides both modes ----------------------------------------

    def test_engram_root_overrides_workspace(self):
        self._touch(".engram-root")
        sub = self._mkdir("a")
        r = resolve_project(str(sub), MARKERS, project_dir=str(sub))
        self.assertEqual(r["path"], str(self.root))  # sentinel pins the higher root

    def test_engram_root_overrides_marker(self):
        self._touch(".engram-root")
        self._touch("pkg", "pyproject.toml")
        r = resolve_project(str(self.root / "pkg"), MARKERS, identity="marker")
        self.assertEqual(r["path"], str(self.root))

    def test_nearest_sentinel_wins(self):
        self._touch(".engram-root")
        self._touch("a", ".engram-root")
        deep = self._mkdir("a", "b")
        r = resolve_project(str(deep), MARKERS)
        self.assertEqual(r["path"], str(self.root / "a"))  # nearest sentinel, not the outer one

    # --- keys are collision-free --------------------------------------------------

    def test_same_name_folders_get_distinct_keys(self):
        a = self._mkdir("x", "backend")
        b = self._mkdir("y", "backend")
        ra = resolve_project(str(a), MARKERS)
        rb = resolve_project(str(b), MARKERS)
        self.assertEqual(ra["label"], rb["label"])  # same human label…
        self.assertNotEqual(ra["key"], rb["key"])  # …but distinct keys (path-hashed)


class ConfigIdentityTests(unittest.TestCase):
    """Config resolves identity mode and the workspace root from env."""

    def _cfg(self, env):
        from core.config import get_config

        with mock.patch.dict(os.environ, env, clear=False):
            return get_config()

    def test_identity_defaults_to_workspace(self):
        # Strip any ambient override so the default is what's asserted.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENGRAM_IDENTITY", None)
            os.environ.pop("CLAUDE_PLUGIN_OPTION_identity", None)
            from core.config import get_config

            self.assertEqual(get_config().identity, "workspace")

    def test_identity_marker_from_env(self):
        self.assertEqual(self._cfg({"ENGRAM_IDENTITY": "marker"}).identity, "marker")

    def test_project_dir_from_claude_project_dir(self):
        self.assertEqual(self._cfg({"CLAUDE_PROJECT_DIR": "/ws/moj-sak"}).project_dir, "/ws/moj-sak")

    def test_project_dir_none_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            from core.config import get_config

            self.assertIsNone(get_config().project_dir)


if __name__ == "__main__":
    unittest.main()
