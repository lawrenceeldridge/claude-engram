"""Service health (core/health.py) — the checks `engram doctor`, the viewer and `engram import` share."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from _harness import temp_data_dir

from core import health
from core.config import get_config
from core.store import Store


class _Store:
    """The two Store reads the scan check makes, over a fixed project table."""

    def __init__(self, projects: list[tuple[str, str, int]], dims: dict[str, set[int]]):
        self._projects = [{"project_key": k, "project_label": label, "c": c} for k, label, c in projects]
        self._dims = dims

    def projects(self):
        return self._projects

    def stored_dims(self, project_key):
        return self._dims.get(project_key, set())


class ServiceCheckTests(unittest.TestCase):
    def setUp(self):
        temp_data_dir(self)
        self.cfg = replace(get_config(), embedding="hash", distiller="heuristic")

    def test_stdlib_defaults_are_all_ok(self):
        checks = health.checks(self.cfg)
        self.assertEqual([c.name for c in checks], ["queue", "embedding", "distiller"])
        self.assertEqual({c.state for c in checks}, {"ok"})
        self.assertEqual(health.queue_check().backend, "inproc")

    def test_an_unreachable_http_distiller_warns_and_names_the_fallback(self):
        check = health.distiller_check(replace(self.cfg, distiller="ollama", distiller_base_url="http://127.0.0.1:1"))
        self.assertEqual((check.backend, check.state), ("ollama", "warn"))
        self.assertIn("heuristic", check.detail)

    def test_the_claude_distiller_is_judged_by_its_cli_not_the_http_url(self):
        cfg = replace(self.cfg, distiller="claude", distiller_cmd="claude", distiller_base_url="http://127.0.0.1:1")
        with mock.patch.object(health.shutil, "which", return_value="/usr/local/bin/claude"):
            self.assertEqual(health.distiller_check(cfg).state, "ok")  # the dead Ollama URL is irrelevant
        with mock.patch.object(health.shutil, "which", return_value=None):
            check = health.distiller_check(cfg)
        self.assertEqual(check.state, "warn")
        self.assertIn("not on PATH", check.detail)

    def test_tcp_probe_fails_open(self):
        self.assertFalse(health.tcp_ok("http://127.0.0.1:1", timeout=0.2))
        self.assertFalse(health.tcp_ok("not-a-url", timeout=0.2))
        self.assertFalse(health.tcp_ok("http://", timeout=0.2))


class ScanCheckTests(unittest.TestCase):
    def setUp(self):
        temp_data_dir(self)
        self.python_cfg = replace(get_config(), scorer="python")

    def test_numpy_is_ok_whatever_the_size(self):
        with mock.patch.object(health, "get_scorer", return_value=object()):  # anything but the pure-Python scorer
            check = health.scan_check(get_config(), _Store([("k", "big", 10**6)], {"k": {768}}))
        self.assertEqual((check.backend, check.state), ("numpy", "ok"))

    def test_warns_above_the_threshold_and_not_below(self):
        at = round(
            health.SCAN_WARN_SECONDS * 1e9 / health.PY_SCAN_NS_PER_ELEMENT / 768
        )  # facts at the threshold, 768 dims
        below = health.scan_check(self.python_cfg, _Store([("k", "proj", at - 100)], {"k": {768}}))
        above = health.scan_check(self.python_cfg, _Store([("k", "proj", at + 100)], {"k": {768}}))
        self.assertEqual((below.backend, below.state), ("python", "ok"))
        self.assertEqual((above.backend, above.state), ("python", "warn"))
        self.assertIn("proj", above.detail)
        self.assertIn("numpy", above.detail)

    def test_the_estimate_scales_with_dim_so_hash_stores_get_more_room(self):
        facts = 50_000
        self.assertEqual(health.scan_check(self.python_cfg, _Store([("k", "p", facts)], {"k": {256}})).state, "ok")
        self.assertEqual(health.scan_check(self.python_cfg, _Store([("k", "p", facts)], {"k": {768}})).state, "warn")
        self.assertAlmostEqual(health.scan_seconds(144_000, 768), 11.7, delta=0.2)  # measured: 11.9 s on the hook

    def test_judges_the_largest_project_or_the_one_named(self):
        store = _Store([("small", "s", 100), ("big", "b", 60_000)], {"small": {768}, "big": {768}})
        self.assertEqual(health.scan_check(self.python_cfg, store).state, "warn")
        self.assertEqual(health.scan_check(self.python_cfg, store, project_key="small").state, "ok")
        self.assertEqual(health.scan_check(self.python_cfg, _Store([], {})).detail, "no facts yet")

    def test_runs_against_a_real_store(self):
        store = Store(get_config().db_path)
        self.addCleanup(store.close)
        self.assertEqual(health.checks(self.python_cfg, store)[-1].name, "scan")


if __name__ == "__main__":
    unittest.main()
