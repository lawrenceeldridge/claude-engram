"""Idea #1 — focus (top_k, injected) vs activated LTM (activated_k, on-demand).

The on-demand `recall` path searches the broader activated_k breadth; the injected
per-turn focus stays at top_k. activated_k defaults to top_k, so the split is inert
until raised.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from core import service
from core.config import get_config
from core.ports.embedding import HashEmbedding
from core.store import Store


class ActivatedKConfigTests(unittest.TestCase):
    def test_defaults_to_top_k(self):
        # Inert by default: no independent breadth unless deliberately raised.
        cfg = get_config()
        self.assertEqual(cfg.activated_k, cfg.top_k)

    def test_env_widens_activated_only(self):
        os.environ["ENGRAM_TOP_K"] = "2"
        os.environ["ENGRAM_ACTIVATED_K"] = "7"
        try:
            cfg = get_config()
            self.assertEqual(cfg.top_k, 2)
            self.assertEqual(cfg.activated_k, 7)
        finally:
            del os.environ["ENGRAM_TOP_K"]
            del os.environ["ENGRAM_ACTIVATED_K"]

    def test_activated_k_never_below_top_k(self):
        os.environ["ENGRAM_TOP_K"] = "5"
        os.environ["ENGRAM_ACTIVATED_K"] = "0"  # manifest default 0 => fall back to top_k
        try:
            cfg = get_config()
            self.assertEqual(cfg.activated_k, 5)
        finally:
            del os.environ["ENGRAM_TOP_K"]
            del os.environ["ENGRAM_ACTIVATED_K"]


class ActivatedKRecallTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.mkdtemp(prefix="engram-test-actk-")
        store = Store(Path(tmp) / "m.db")
        return store, {"key": "actk", "path": tmp, "label": "actk"}

    def test_on_demand_recall_exceeds_the_injected_focus(self):
        # Focus of 1, activated breadth of 5: the on-demand recall path returns more than
        # the injected focus would, proving activated_k (not top_k) governs it.
        base = get_config()
        cfg = replace(base, top_k=1, activated_k=5, min_sim=-1.0, recall_min_confidence=0.0)
        embedder = HashEmbedding(dim=cfg.dim)
        store, project = self._store()
        facts = [
            "widget alpha handles login",
            "widget beta handles billing",
            "widget gamma handles search",
            "widget delta handles email",
            "widget epsilon handles logging",
        ]
        service.add_facts(store, embedder, cfg, project, "s", facts)
        result = service.recall_structured(store, embedder, cfg, project, "widget")
        store.close()
        # k defaulted to activated_k=5, so more than the top_k=1 focus is matched.
        self.assertGreater(result["matched"], cfg.top_k)

    def test_explicit_k_still_overrides(self):
        base = get_config()
        cfg = replace(base, top_k=1, activated_k=5, min_sim=-1.0, recall_min_confidence=0.0)
        embedder = HashEmbedding(dim=cfg.dim)
        store, project = self._store()
        service.add_facts(store, embedder, cfg, project, "s", [f"widget {w} entry" for w in "abcde"])
        result = service.recall_structured(store, embedder, cfg, project, "widget", k=2)
        store.close()
        self.assertLessEqual(result["matched"], 2)


if __name__ == "__main__":
    unittest.main()
