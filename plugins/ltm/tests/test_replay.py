"""Fixture-transcript tests for the replay counterfactual core (bench/replay.py).

All pure — the index answer is injected, no store or embedder involved.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench.replay import counterfactual, find_sweeps, parse_transcript, sweep_query  # noqa: E402


def _tool_use(name: str, tool_id: str, **tool_input) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]},
        }
    )


def _tool_result(tool_id: str, text: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
        }
    )


def _sweep_lines() -> list[str]:
    """A grep -> grep -> whole-file read sweep, then an unrelated Edit call."""
    return [
        _tool_use("Grep", "t1", pattern="render_block"),
        _tool_result("t1", "x" * 4000),
        _tool_use("Grep", "t2", pattern="min_sim"),
        _tool_result("t2", "y" * 2000),
        _tool_use("Read", "t3", file_path="/repo/core/recall.py"),
        _tool_result("t3", "z" * 20000),
        _tool_use("Edit", "t4", file_path="/repo/core/recall.py"),
        _tool_result("t4", "ok"),
    ]


class ParseTests(unittest.TestCase):
    def test_events_carry_result_bytes(self):
        events = parse_transcript(_sweep_lines())
        self.assertEqual([e["name"] for e in events], ["Grep", "Grep", "Read", "Edit"])
        self.assertEqual(events[0]["result_bytes"], 4000)
        self.assertEqual(events[2]["result_bytes"], 20000)

    def test_malformed_lines_are_skipped(self):
        lines = ["not json", '{"half":', *_sweep_lines()]
        self.assertEqual(len(parse_transcript(lines)), 4)

    def test_list_content_results_are_summed(self):
        lines = [
            _tool_use("Grep", "a1", pattern="x"),
            json.dumps(
                {
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "a1",
                                "content": [{"type": "text", "text": "abc"}, {"type": "text", "text": "de"}],
                            }
                        ]
                    }
                }
            ),
        ]
        self.assertEqual(parse_transcript(lines)[0]["result_bytes"], 5)


class SweepTests(unittest.TestCase):
    def test_finds_sweep_ending_on_whole_file_read(self):
        sweeps = find_sweeps(parse_transcript(_sweep_lines()))
        self.assertEqual(len(sweeps), 1)
        self.assertEqual(sweeps[0]["landing"], "/repo/core/recall.py")
        self.assertEqual(sweeps[0]["cost_bytes"], 26000)

    def test_bounded_read_breaks_the_sweep(self):
        lines = [
            _tool_use("Grep", "b1", pattern="x"),
            _tool_result("b1", "r"),
            _tool_use("Read", "b2", file_path="/repo/a.py", offset=10, limit=20),
            _tool_result("b2", "r"),
        ]
        self.assertEqual(find_sweeps(parse_transcript(lines)), [])

    def test_single_call_is_not_a_sweep(self):
        lines = [
            _tool_use("Read", "c1", file_path="/repo/a.py"),
            _tool_result("c1", "r" * 100),
        ]
        self.assertEqual(find_sweeps(parse_transcript(lines)), [])

    def test_sweep_without_landing_read_is_dropped(self):
        lines = [
            _tool_use("Grep", "d1", pattern="x"),
            _tool_result("d1", "r"),
            _tool_use("Glob", "d2", pattern="**/*.py"),
            _tool_result("d2", "r"),
        ]
        self.assertEqual(find_sweeps(parse_transcript(lines)), [])


class QueryAndCreditTests(unittest.TestCase):
    def setUp(self):
        self.sweep = find_sweeps(parse_transcript(_sweep_lines()))[0]

    def test_query_uses_patterns_and_landing_stem(self):
        q = sweep_query(self.sweep)
        self.assertIn("render_block", q)
        self.assertIn("min_sim", q)
        self.assertIn("recall", q)

    def test_creditable_when_landing_in_hits(self):
        result = counterfactual(self.sweep, ["core/recall.py"], outline_bytes=600, body_bytes=1400)
        self.assertTrue(result["creditable"])
        self.assertEqual(result["actual_tokens"], 26000 // 4)
        # 600 + 1400 + 2*200 = 2400 bytes -> 600 tokens
        self.assertEqual(result["indexed_tokens"], 600)
        self.assertEqual(result["saved_tokens"], (26000 - 2400) // 4)

    def test_not_creditable_when_index_misses(self):
        result = counterfactual(self.sweep, ["core/store.py"], outline_bytes=600, body_bytes=1400)
        self.assertFalse(result["creditable"])
        self.assertEqual(result["saved_tokens"], 0)

    def test_empty_hits_never_credit(self):
        result = counterfactual(self.sweep, [], outline_bytes=0, body_bytes=0)
        self.assertFalse(result["creditable"])


if __name__ == "__main__":
    unittest.main()
