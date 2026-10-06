"""Verbal intake path (Phase 4) — the conversation delta enters the A-S sensory register as a
'verbal' observation, ADDITIVELY, without changing distillation's `facts` output (the parity
gate). Stdlib unittest, heuristic distiller, hash embedder, no network.

A-S: conversation is the verbal sensory input; distillation is the coding/attention control
process that transfers the worthy parts to facts (STS). Recording the raw perception must never
alter what gets distilled — so the same transcript yields the same facts whether sensory is on or
off. That equality is the load-bearing test of this phase.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _harness  # noqa: F401

from core import service
from core.config import get_config
from core.ports.embedding import HashEmbedding
from core.store import Store


def _turn(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}) + "\n"


_TRANSCRIPT = _turn("user", "where does the app deploy?") + _turn(
    "assistant", "The deploy target is fly.io. The database is Postgres and CI runs on GitHub Actions."
)


class VerbalIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        self.tf.write(_TRANSCRIPT)
        self.tf.flush()
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        os.unlink(self.tf.name)
        self.tmp.cleanup()

    def _capture(self, sensory_enabled: bool):
        db = Path(self.tmp.name) / f"mem-{sensory_enabled}.db"  # fresh store per run
        store = Store(db)
        cfg = replace(get_config(), distiller="heuristic", embedding="hash", sensory_enabled=sensory_enabled)
        embedder = HashEmbedding(dim=cfg.dim)
        service.capture_transcript_incremental(store, embedder, cfg, self.project, "sess-1", self.tf.name)
        facts = sorted(r["text"] for r in store.db.execute("SELECT text FROM facts WHERE project_key = ?", ("proj",)))
        sensory = store.sensory_rows("proj")
        store.close()
        return facts, sensory

    def test_facts_output_identical_with_sensory_on_or_off(self):
        facts_on, _ = self._capture(True)
        facts_off, _ = self._capture(False)
        self.assertEqual(facts_on, facts_off)  # PARITY — verbal intake changes nothing about facts
        self.assertTrue(facts_on)  # sanity: capture actually produced facts

    def test_verbal_observation_recorded_when_enabled(self):
        _, sensory = self._capture(True)
        verbal = [r for r in sensory if r["modality"] == "verbal"]
        self.assertEqual(len(verbal), 1)
        self.assertIn("fly.io", verbal[0]["text"])
        self.assertEqual(verbal[0]["attended"], 0)  # transient — distillation is the coding gate, not attention here

    def test_no_verbal_observation_when_disabled(self):
        _, sensory = self._capture(False)
        self.assertEqual([r for r in sensory if r["modality"] == "verbal"], [])

    def test_capture_survives_a_broken_sensory_write(self):
        # fail-open: a register write error must not break capture or drop facts
        store = Store(Path(self.tmp.name) / "mem-broken.db")
        cfg = replace(get_config(), distiller="heuristic", embedding="hash", sensory_enabled=True)
        embedder = HashEmbedding(dim=cfg.dim)

        def _boom(*_a, **_k):
            raise RuntimeError("sensory write failed")

        store.add_sensory = _boom  # type: ignore[method-assign]
        try:
            service.capture_transcript_incremental(store, embedder, cfg, self.project, "sess-1", self.tf.name)
            facts = store.db.execute("SELECT COUNT(*) FROM facts WHERE project_key = ?", ("proj",)).fetchone()[0]
        finally:
            store.close()
        self.assertGreater(facts, 0)  # facts captured despite the sensory failure


if __name__ == "__main__":
    unittest.main()
