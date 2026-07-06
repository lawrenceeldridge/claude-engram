"""Idea #6 — LT-WM retrieval structure: the titled session-core scaffold + the cue field."""

from __future__ import annotations

import unittest

from core.ports.distill import DistilledFact
from core.recall import render_scaffold


def _row(fid: str, text: str, title: str = "") -> dict:
    # A dict is a faithful stand-in for sqlite3.Row here: supports row["k"] and row.keys().
    return {"id": fid, "text": text, "title": title}


class ScaffoldRenderTests(unittest.TestCase):
    def test_groups_facts_under_their_title(self):
        hits = [
            (1.0, _row("a", "deploys to lambda", "Deployment")),
            (1.0, _row("b", "staging redeploys on merge", "Deployment")),
            (1.0, _row("c", "postgres on rds", "Data")),
        ]
        block, ids = render_scaffold("Project memory:", hits, 1000)
        self.assertEqual(block.count("Deployment:"), 1)  # grouped, not repeated
        self.assertIn("Data:", block)
        self.assertEqual(set(ids), {"a", "b", "c"})

    def test_untitled_facts_group_under_notes(self):
        block, ids = render_scaffold("Project memory:", [(1.0, _row("a", "loose fact", ""))], 1000)
        self.assertIn("Notes:", block)
        self.assertEqual(ids, ["a"])

    def test_empty_is_null_object(self):
        self.assertEqual(render_scaffold("Project memory:", [], 1000), ("", []))

    def test_char_budget_is_respected(self):
        hits = [(1.0, _row(str(i), "x" * 40, "Topic")) for i in range(30)]
        block, ids = render_scaffold("Project memory:", hits, 150)
        self.assertLessEqual(len(block), 150)
        self.assertLess(len(ids), 30)  # truncated under the cap


class CueFieldTests(unittest.TestCase):
    def test_cue_defaults_empty(self):
        # Null/Special-Case: the heuristic distiller leaves it empty; the field is the
        # stable interface for a future cue-emitting LLM distiller.
        self.assertEqual(DistilledFact(text="a fact").cue, "")

    def test_cue_is_carried_when_set(self):
        self.assertEqual(DistilledFact(text="a fact", cue="deploying to prod").cue, "deploying to prod")


if __name__ == "__main__":
    unittest.main()
