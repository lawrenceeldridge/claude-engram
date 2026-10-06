"""Ingestion quality gate (core/domain/ingest.py) — the pure capture-time policy.

Phase 1 of capture-quality-gate: the module is pure and not yet wired into capture, so
these tests exercise the predicates directly. Stdlib unittest, no network. The bar is
precision over recall — a real decision fact must survive every gate (test_real_fact_*).
"""

from __future__ import annotations

import unittest

import _harness  # noqa: F401

from core.domain import ingest

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


class ActionVocabularyTests(unittest.TestCase):
    """The one definition of a rendered tool action, shared by the renderer, the exchange footer
    and the distiller."""

    def test_every_verb_round_trips(self):
        for verb in ingest.ACTION_VERBS:
            line = ingest.action_line(verb.verb, "thing.py")
            self.assertEqual(ingest.parse_action(line), (verb, "thing.py"), line)
            self.assertTrue(ingest.is_action_line(line))
        self.assertEqual(ingest.ACTION_PREFIXES, tuple(v.prefix for v in ingest.ACTION_VERBS))

    def test_counted_verbs_take_free_text(self):
        verb, argument = ingest.parse_action("Ran: pytest -q tests/test_x.py")
        self.assertEqual((verb.verb, verb.listed, argument), ("Ran", False, "pytest -q tests/test_x.py"))

    def test_the_longer_prefix_wins(self):
        self.assertEqual(ingest.parse_action("Used skill engram-plan")[0].verb, "Used skill")
        self.assertEqual(ingest.parse_action("Used TaskStop")[0].verb, "Used")

    def test_strict_parse_leaves_prose_as_conversation(self):
        for prose in ("Read the docs before you start", "Wrote the design doc and findings report", "Ran: ", "Edited"):
            self.assertIsNone(ingest.parse_action(prose), prose)
            self.assertFalse(ingest.is_action_line(prose))
        self.assertFalse(ingest.is_action_line(REAL_FACT))

    def test_lenient_parse_only_matches_the_verb(self):
        verb, argument = ingest.parse_action("Read My Notes.md", strict=False)
        self.assertEqual((verb.verb, argument), ("Read", "My Notes.md"))

    def test_total_on_bad_input(self):
        for bad in (None, 42, b"Ran: x", ["Edited a.py"]):
            self.assertIsNone(ingest.parse_action(bad))
            self.assertFalse(ingest.is_action_line(bad))

    def test_unknown_verb_is_a_programming_error(self):
        with self.assertRaises(KeyError):
            ingest.action_line("Deleted", "a.py")


class LineSalienceTests(unittest.TestCase):
    def test_first_person_statements_rank_highest(self):
        for line in (
            "I prefer tabs over spaces in this repo.",
            "i'd rather avoid mocks in the store tests",
            "My favourite editor is Helix.",
            "I usually run the full suite before pushing.",
            "I'm allergic to shellfish.",
            "I work as a backend engineer at Acme.",
            "We use pnpm, not npm, in this monorepo.",
            "We decided to keep int8 vectors.",
            "We'll go with SQLite for the queue.",
            "The decision was to drop the daemon.",
        ):
            self.assertEqual(ingest.line_salience(line), ingest.SALIENT, line)

    def test_actions_rank_lowest_and_the_rest_is_plain(self):
        self.assertEqual(ingest.line_salience("Ran: pytest -q"), ingest.ACTION)
        self.assertEqual(ingest.line_salience("Edited auth.py"), ingest.ACTION)
        for line in ("The deploy target is fly.io.", "I used grep to find it.", "Myopia is common.", "Read the docs"):
            self.assertEqual(ingest.line_salience(line), ingest.PLAIN, line)
        self.assertGreater(ingest.SALIENT, ingest.PLAIN)
        self.assertGreater(ingest.PLAIN, ingest.ACTION)

    def test_total_on_bad_input(self):
        for bad in (None, 3, b"I prefer x"):
            self.assertEqual(ingest.line_salience(bad), ingest.PLAIN)


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
