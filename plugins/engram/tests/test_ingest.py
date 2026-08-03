"""Ingestion quality gate (core/domain/ingest.py) — the pure capture-time policy.

Phase 1 of capture-quality-gate: the module is pure and not yet wired into capture, so
these tests exercise the predicates directly. Stdlib unittest, no network. The bar is
precision over recall — a real decision fact must survive every gate (test_real_fact_*).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.domain import ingest  # noqa: E402

# A genuine, durable fact. It must pass EVERY gate (never dropped) — the over-filtering guard.
REAL_FACT = "We chose an int8 pre-filter before the float cosine rescore because it passed the benchmark at 0.92 MRR."


class StripHarnessBlocksTests(unittest.TestCase):
    def test_strips_system_reminder(self):
        out = ingest.strip_harness_blocks("before <system-reminder>noise here</system-reminder> after")
        self.assertNotIn("noise here", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_strips_task_notification_multiline(self):
        text = "keep <task-notification>\nagent X finished\npayload blob\n</task-notification> tail"
        out = ingest.strip_harness_blocks(text)
        self.assertNotIn("agent X finished", out)
        self.assertNotIn("payload blob", out)
        self.assertIn("keep", out)
        self.assertIn("tail", out)

    def test_strips_tag_with_attributes(self):
        out = ingest.strip_harness_blocks('a <task-notification id="7">x</task-notification> b')
        self.assertEqual(out, "a  b")  # block removed; inner double-space kept, outer trimmed

    def test_non_str_returns_empty(self):
        self.assertEqual(ingest.strip_harness_blocks(None), "")
        self.assertEqual(ingest.strip_harness_blocks(123), "")

    def test_leaves_real_text_untouched(self):
        self.assertEqual(ingest.strip_harness_blocks(REAL_FACT), REAL_FACT)


class HarnessNoiseTests(unittest.TestCase):
    def test_task_notification_is_noise(self):
        self.assertTrue(ingest.is_harness_noise("<task-notification>agent done</task-notification>"))

    def test_command_and_ide_wrappers_are_noise(self):
        self.assertTrue(ingest.is_harness_noise("<command-name>/foo</command-name>"))
        self.assertTrue(ingest.is_harness_noise("<ide_opened_file>x.ts</ide_opened_file>"))
        self.assertTrue(ingest.is_harness_noise("Caveat: the messages below…"))

    def test_real_user_text_is_not_noise(self):
        self.assertFalse(ingest.is_harness_noise("Please paginate the consolidation panel."))
        self.assertFalse(ingest.is_harness_noise(REAL_FACT))

    def test_non_str_is_not_noise(self):
        self.assertFalse(ingest.is_harness_noise(None))


class UserAskAndNarrationTests(unittest.TestCase):
    def test_user_ask(self):
        self.assertTrue(ingest.is_user_ask("can you paginate this?"))
        self.assertTrue(ingest.is_user_ask("What is the STM capacity?"))
        self.assertFalse(ingest.is_user_ask(REAL_FACT))
        self.assertFalse(ingest.is_user_ask(None))

    def test_narration(self):
        self.assertTrue(ingest.is_narration("Let me check the store schema."))
        self.assertTrue(ingest.is_narration("Here's the plan:"))
        self.assertFalse(ingest.is_narration("The delete dialog now uses an AlertDialog."))
        self.assertFalse(ingest.is_narration(None))


class EphemeralStatusTests(unittest.TestCase):
    def test_ci_build_status_dropped(self):
        for line in (
            "ESLint passed",
            "TypeScript compilation passed",
            "production build succeeded",
            "working tree clean",
            "All checks passed!",
            "TruffleHog no secrets",
            "#291/#292 merged",
            "tests failed",
        ):
            self.assertTrue(ingest.is_ephemeral_status(line), f"should be ephemeral: {line!r}")

    def test_real_fact_mentioning_a_status_word_is_kept(self):
        # Long + substance after context: not a bare verdict → kept.
        self.assertFalse(ingest.is_ephemeral_status(REAL_FACT))
        self.assertFalse(ingest.is_ephemeral_status("the build pipeline was rewritten to use Vite"))
        self.assertFalse(ingest.is_ephemeral_status("the migration passed review and shipped to staging"))

    def test_word_cap_and_non_str(self):
        self.assertFalse(ingest.is_ephemeral_status("a b c d e f g passed"))  # > max_words
        self.assertFalse(ingest.is_ephemeral_status(None))
        self.assertFalse(ingest.is_ephemeral_status(""))


class TrivialPromptTests(unittest.TestCase):
    def test_bare_confirmations_dropped(self):
        for p in ("yes", "Apply all", "Option C", "ok", "go ahead", "commit it", "lgtm", "d"):
            self.assertTrue(ingest.is_trivial_prompt(p), f"should be trivial: {p!r}")

    def test_short_directive_prompts_dropped(self):
        for p in ("yes lets commit", "apply the fix", "push it now", "run the tests"):
            self.assertTrue(ingest.is_trivial_prompt(p), f"should be trivial: {p!r}")

    def test_slash_command_echo_dropped(self):
        self.assertTrue(ingest.is_trivial_prompt("/ukh-world-plan run tracker-x Phase 1"))
        self.assertTrue(ingest.is_trivial_prompt("/engram-git commit"))

    def test_substantive_prompt_kept(self):
        self.assertFalse(ingest.is_trivial_prompt("Paginate the consolidation panel and add a loader."))
        self.assertFalse(ingest.is_trivial_prompt("go with option C because it is simpler and reversible"))
        self.assertFalse(ingest.is_trivial_prompt(REAL_FACT))

    def test_min_len_threshold(self):
        self.assertTrue(ingest.is_trivial_prompt("fix bug", min_len=12))  # 7 < 12
        self.assertFalse(ingest.is_trivial_prompt("fix the flaky recall test", min_len=12))

    def test_non_str_is_kept(self):
        self.assertFalse(ingest.is_trivial_prompt(None))


class LowValueFactTests(unittest.TestCase):
    """The composite import/retro-sweep gate — harness OR ephemeral OR trivial-content, and
    crucially NO length threshold, so a short-but-real imported fact survives."""

    def test_drops_harness_ephemeral_and_trivial_content(self):
        self.assertTrue(ingest.is_low_value_fact("<task-notification>agent done</task-notification>"))
        self.assertTrue(ingest.is_low_value_fact("TypeScript compilation passed"))
        self.assertTrue(ingest.is_low_value_fact("Option C"))
        self.assertTrue(ingest.is_low_value_fact("/engram-git commit"))

    def test_keeps_real_facts_including_short_ones(self):
        self.assertFalse(ingest.is_low_value_fact(REAL_FACT))
        self.assertFalse(ingest.is_low_value_fact("The deploy target is fly.io."))
        # Short but real — must NOT be dropped (no length threshold on facts).
        for terse in ("Use int8 vectors", "Node 20", "React 18", "x", "keep"):
            self.assertFalse(ingest.is_low_value_fact(terse), f"short real fact dropped: {terse!r}")

    def test_non_str_is_kept(self):
        self.assertFalse(ingest.is_low_value_fact(None))


class LowValueMemoryTests(unittest.TestCase):
    """The retro-sweep dispatch — a stored row judged by its kind's live-door policy."""

    def test_prompt_rows_use_the_prompt_gate(self):
        # "yes lets commit" isn't caught by the fact gate, but IS a trivial prompt.
        self.assertTrue(ingest.is_low_value_memory("prompt", "yes lets commit"))
        self.assertFalse(ingest.is_low_value_fact("yes lets commit"))  # fact gate leaves it

    def test_fact_rows_use_the_fact_gate(self):
        self.assertTrue(ingest.is_low_value_memory("discovery", "TypeScript compilation passed"))
        self.assertFalse(ingest.is_low_value_memory("discovery", "Use int8 vectors"))  # short real fact kept

    def test_prompt_gate_disabled_at_zero(self):
        self.assertFalse(ingest.is_low_value_memory("prompt", "yes lets commit", min_prompt_len=0))


class RealFactSurvivesEveryGateTests(unittest.TestCase):
    """The over-filtering guard: a genuine decision fact must clear every predicate."""

    def test_real_fact_is_never_dropped(self):
        self.assertFalse(ingest.is_harness_noise(REAL_FACT))
        self.assertFalse(ingest.is_user_ask(REAL_FACT))
        self.assertFalse(ingest.is_narration(REAL_FACT))
        self.assertFalse(ingest.is_ephemeral_status(REAL_FACT))
        self.assertFalse(ingest.is_trivial_prompt(REAL_FACT))
        self.assertFalse(ingest.is_low_value_fact(REAL_FACT))
        self.assertEqual(ingest.strip_harness_blocks(REAL_FACT), REAL_FACT)


if __name__ == "__main__":
    unittest.main()
