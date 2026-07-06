"""Project-identity resolution — marker walk + `.engram-root` sentinel precedence.

Stdlib unittest, no network. Verifies that a nested package resolves to its own
marker by default, but that an explicit `.engram-root` sentinel pins a higher root so
subfolders (a plugin package, an app's backend/frontend) collapse to one project.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_nearest_marker_wins_without_sentinel(self):
        self._touch(".git")
        self._touch("pkg", "pyproject.toml")
        pkg = self.root / "pkg"
        r = resolve_project(str(pkg), MARKERS)
        self.assertEqual(r["path"], str(pkg))  # nested package is its own project
        self.assertEqual(r["label"], "pkg")

    def test_ltm_root_sentinel_pins_root(self):
        self._touch(".engram-root")
        self._touch("pkg", "pyproject.toml")
        r = resolve_project(str(self.root / "pkg"), MARKERS)
        self.assertEqual(r["path"], str(self.root))  # sentinel overrides the nearer marker
        self.assertEqual(r["label"], self.root.name)

    def test_nearest_sentinel_wins(self):
        self._touch(".engram-root")
        self._touch("a", ".engram-root")
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True, exist_ok=True)
        r = resolve_project(str(deep), MARKERS)
        self.assertEqual(r["path"], str(self.root / "a"))  # nearest sentinel, not the outer one

    def test_same_key_from_any_subdir_under_sentinel(self):
        self._touch(".engram-root")
        self._touch("pkg", "pyproject.toml")
        a = resolve_project(str(self.root / "pkg"), MARKERS)
        b = resolve_project(str(self.root), MARKERS)
        self.assertEqual(a["key"], b["key"])  # both collapse to the sentinel root

    def test_falls_back_to_start_when_no_marker(self):
        sub = self.root / "x"
        sub.mkdir()
        r = resolve_project(str(sub), MARKERS)
        self.assertEqual(r["path"], str(sub))


if __name__ == "__main__":
    unittest.main()
