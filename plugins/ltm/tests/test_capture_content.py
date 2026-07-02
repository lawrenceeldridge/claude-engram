"""Capture-content tests — action-aware extraction + outcome-biased distillation.

Guards the 0.6.0 fix: memory must record what the assistant *did* (tool actions),
not just prompts, and must drop harness scaffolding and user questions.

Run: python3 -m unittest discover -s plugins/ltm/tests
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.distill import _MAX_INPUT_CHARS, _clip, heuristic_facts  # noqa: E402
from core.transcript import extract_text  # noqa: E402


def _write_transcript(entries: list[dict]) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for entry in entries:
        fh.write(json.dumps(entry) + "\n")
    fh.close()
    return fh.name


def _assistant(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def _user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


class ExtractionTests(unittest.TestCase):
    def test_tool_use_becomes_action_lines(self):
        text = extract_text(_write_transcript([
            _assistant([
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/src/auth.py"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "just test && echo done"}},
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "delete dialog"}},
            ]),
        ]))
        self.assertIn("Edited auth.py", text)
        self.assertIn("Ran: just test", text)
        self.assertIn("Searched for delete dialog", text)

    def test_thinking_and_tool_result_dropped(self):
        text = extract_text(_write_transcript([
            _assistant([
                {"type": "thinking", "thinking": "secret private reasoning that must not persist"},
                {"type": "text", "text": "Fixed the RTL alignment bug in the dialog."},
            ]),
            _user([{"type": "tool_result", "tool_use_id": "x", "content": "verbose 5000-line output"}]),
        ]))
        self.assertIn("Fixed the RTL alignment bug in the dialog.", text)
        self.assertNotIn("secret private reasoning", text)
        self.assertNotIn("verbose 5000-line output", text)

    def test_harness_scaffolding_stripped(self):
        text = extract_text(_write_transcript([
            _user("<command-name>/clear</command-name>"),
            _user("<ide_opened_file>The user opened /repo/x.ts</ide_opened_file>"),
            _user("Real request: refactor the ingestion client."),
            _user("Answer <system-reminder>injected junk</system-reminder>please."),
        ]))
        self.assertNotIn("command-name", text)
        self.assertNotIn("ide_opened_file", text)
        self.assertNotIn("injected junk", text)
        self.assertIn("Real request: refactor the ingestion client.", text)

    def test_todowrite_is_not_recorded(self):
        text = extract_text(_write_transcript([
            _assistant([{"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}}]),
        ]))
        self.assertEqual(text.strip(), "")


class HeuristicBiasTests(unittest.TestCase):
    def test_action_lines_survive_and_questions_dropped(self):
        text = "\n".join([
            "Can we have it so localhost is always available?",
            "yes lets also do the above",
            "Edited AttachmentField.tsx",
            "Ran: npm run build",
            "The delete dialog now uses an in-app AlertDialog.",
        ])
        facts = heuristic_facts(text)
        self.assertIn("Edited AttachmentField.tsx", facts)
        self.assertIn("Ran: npm run build", facts)
        self.assertIn("The delete dialog now uses an in-app AlertDialog.", facts)
        self.assertFalse(any(f.endswith("?") for f in facts))
        self.assertNotIn("yes lets also do the above", facts)

    def test_residual_tag_lines_dropped(self):
        facts = heuristic_facts("<command-args></command-args>\nWrote mcp_server.py for the recall tool.")
        self.assertNotIn("<command-args></command-args>", facts)
        self.assertIn("Wrote mcp_server.py for the recall tool.", facts)

    def test_assistant_narration_dropped_but_outcomes_kept(self):
        text = "\n".join([
            "Let me check what the auto-index already did, and the scale:",
            "The assistant can now map Linear tickets to code.",
            "I'll now update the tool description.",
            "Now let me read the indexer.",
            "The delete dialog now uses an in-app AlertDialog.",
            "search_code indexes TypeScript via tree-sitter.",
        ])
        facts = heuristic_facts(text)
        self.assertNotIn("The assistant can now map Linear tickets to code.", facts)
        self.assertNotIn("I'll now update the tool description.", facts)
        self.assertNotIn("Now let me read the indexer.", facts)
        self.assertFalse(any(f.endswith(":") for f in facts))
        self.assertIn("The delete dialog now uses an in-app AlertDialog.", facts)
        self.assertIn("search_code indexes TypeScript via tree-sitter.", facts)


class ClipTests(unittest.TestCase):
    def test_small_input_unchanged(self):
        text = "a short delta"
        self.assertEqual(_clip(text), text)

    def test_oversized_input_bounded_with_marker(self):
        text = "H" * 5000 + "M" * 200000 + "T" * 5000
        clipped = _clip(text)
        self.assertLessEqual(len(clipped), _MAX_INPUT_CHARS + 60)
        self.assertIn("characters omitted", clipped)

    def test_clip_keeps_head_and_tail(self):
        text = "HEADSTART" + "x" * 100000 + "TAILEND"
        clipped = _clip(text)
        self.assertTrue(clipped.startswith("HEADSTART"))
        self.assertTrue(clipped.endswith("TAILEND"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
