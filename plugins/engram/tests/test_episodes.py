"""Episodic units + non-file chunk indexing — stdlib, hash embedder.

``exchange_units`` is the verbatim unit capture stores and the LongMemEval benchmark measures, so
its grouping/splitting and the actions footer are pinned here, as is ``refold_exchanges`` (the
one-off rewrite of exchanges stored before the footer). ``index_nonfile`` /
``Store.replace_nonfile_chunks`` are the one writer shared by snapshot and exchange chunks.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _harness  # noqa: F401

from core.config import get_config
from core.domain.episodes import (
    EXCHANGE_MAX_CHARS,
    FOOTER_MAX_CHARS,
    Exchange,
    action_footer,
    exchange_units,
    legacy_turns,
    prepare_exchanges,
    refold_exchanges,
    should_keep_exchange,
)
from core.domain.privacy import REDACTED
from core.index.indexer import (
    _records_from_units,
    exchange_anchor,
    exchange_chunk_units,
    exchange_position,
    index_nonfile,
    index_snapshot,
)
from core.ports.embedding import HashEmbedding
from core.store import Store


class ExchangeUnitTests(unittest.TestCase):
    def test_user_turn_opens_an_exchange_and_assistant_turns_join_it(self):
        turns = [("user", "hi"), ("assistant", "hello"), ("assistant", "anything else?"), ("user", "bye")]
        self.assertEqual(
            exchange_units(turns),
            [
                Exchange(0, 0, "User: hi\nAssistant: hello\nAssistant: anything else?"),
                Exchange(1, 0, "User: bye"),
            ],
        )

    def test_leading_assistant_turns_form_exchange_zero(self):
        self.assertEqual(
            exchange_units([("assistant", "welcome"), ("user", "q")])[0], Exchange(0, 0, "Assistant: welcome")
        )

    def test_other_roles_and_empty_turns_are_skipped(self):
        self.assertEqual(exchange_units([("tool", "x"), ("user", "  "), ("system", "y")]), [])

    def test_long_exchange_splits_on_lines_within_the_cap(self):
        turns = [("user", "q"), ("assistant", "\n".join(["line " + "x" * 90] * 20))]
        units = exchange_units(turns)
        self.assertGreater(len(units), 1)
        self.assertTrue(all(len(u.text) <= EXCHANGE_MAX_CHARS for u in units))
        self.assertEqual([u.part for u in units], list(range(len(units))))
        self.assertEqual({u.turn for u in units}, {0})

    def test_single_overlong_line_is_hard_split(self):
        units = exchange_units([("user", "y" * 2000)], max_chars=800)
        self.assertEqual([len(u.text) for u in units], [800, 800, 406])  # "User: " + 2000 chars

    def test_stable_across_runs(self):
        turns = [("user", "alpha"), ("assistant", "beta " * 300)]
        self.assertEqual(exchange_units(turns), exchange_units(turns))


class ActionFooterTests(unittest.TestCase):
    def test_grouped_by_verb_in_first_use_order(self):
        actions = ["Read auth.py", "Edited auth.py", "Ran: pytest -q", "Edited test_auth.py", "Edited auth.py"]
        self.assertEqual(
            action_footer(actions), "Actions: Read auth.py · Edited auth.py, test_auth.py · Ran: pytest -q"
        )

    def test_counted_verbs_give_a_count_and_the_first_few(self):
        runs = [f"Ran: step {i}" for i in range(12)]
        self.assertEqual(action_footer(runs), "Actions: Ran 12: step 0; step 1; step 2 (+9)")
        self.assertEqual(action_footer(["Ran: a", "Ran: b"]), "Actions: Ran 2: a; b")

    def test_no_actions_is_no_footer(self):
        self.assertEqual(action_footer([]), "")
        self.assertEqual(action_footer(["", ""]), "")

    def test_capped_with_a_count_of_what_was_left_out(self):
        actions = [f"Edited module_{i:03d}.py" for i in range(200)] + ["Ran: just test", "Called mcp__x"]
        footer = action_footer(actions)
        self.assertLessEqual(len(footer), FOOTER_MAX_CHARS)
        shown = footer.removeprefix("Actions: Edited ").split(" (+")[0].split(", ")
        self.assertTrue(footer.endswith(f"(+{202 - len(shown)} more)"))  # unshown edits + the two later groups

    def test_a_group_that_cannot_fit_is_dropped_whole(self):
        footer = action_footer(["Ran: " + "x" * 70, "Ran: " + "y" * 70], max_chars=60)
        self.assertEqual(footer, "Actions: (+2 more)")


class ExchangeFoldTests(unittest.TestCase):
    def test_actions_fold_into_one_footer_on_the_first_part(self):
        turns = [
            ("user", "please fix the failing test"),
            ("assistant", "Looking."),
            ("action", "Read auth.py"),
            ("action", "Ran: pytest -q"),
            ("assistant", "\n".join(["detail " + "x" * 90] * 12)),
            ("action", "Edited auth.py"),
        ]
        units = exchange_units(turns)
        self.assertGreater(len(units), 1)
        self.assertTrue(units[0].text.endswith("\nActions: Read auth.py · Ran: pytest -q · Edited auth.py"))
        self.assertFalse(any("Actions:" in u.text for u in units[1:]))
        self.assertFalse(any(line.startswith(("Ran: ", "Edited ")) for u in units for line in u.text.split("\n")))
        self.assertTrue(all(len(u.text) <= EXCHANGE_MAX_CHARS for u in units[1:]))  # the cap bounds conversation

    def test_an_exchange_of_actions_alone_forms_no_unit(self):
        turns = [("user", "first question here"), ("action", "Ran: ls"), ("action", "Read a.py")]
        self.assertEqual(
            exchange_units(turns), [Exchange(0, 0, "User: first question here\nActions: Ran: ls · Read a.py")]
        )
        units = exchange_units([("action", "Ran: ls"), ("user", "next question")])  # leading actions: exchange 0
        self.assertEqual(units, [Exchange(1, 0, "User: next question")])

    def test_conversation_without_actions_is_unchanged(self):
        # LongMemEval sessions carry no actions: the benchmark's units are exactly what they were.
        turns = [("user", "hi"), ("assistant", "hello"), ("user", "bye")]
        self.assertEqual(
            exchange_units(turns), [Exchange(0, 0, "User: hi\nAssistant: hello"), Exchange(1, 0, "User: bye")]
        )


OLD_EPISODE = [  # an exchange as stored before the footer — a long tool run spills into its own part
    (0, 0, "User: run the suite and fix what fails\nAssistant: Running it.\nRan: pytest -q"),
    (0, 1, "Ran: pytest -q tests/test_a.py\nEdited a.py\nUsed TaskStop: {'task_id': 'be4iapsb6'}"),
    (0, 2, "Assistant: Fixed — the fixture leaked state.\nRead the docs before changing it again."),
    (1, 0, "User: thanks, now commit it please\nAssistant: Ran: git commit -m fix"),
]


class RefoldTests(unittest.TestCase):
    def test_legacy_turns_reads_the_old_format(self):
        self.assertEqual(
            legacy_turns(
                "User: Read me first\nAssistant: Done.\nEdited a.py\nUsed Skill: {'skill': 'x'}\nRead the docs"
            ),
            [
                ("user", "Read me first"),  # a user line is never an action
                ("assistant", "Done."),
                ("action", "Edited a.py"),
                ("action", "Used Skill"),  # the old argument dump becomes the bare tool name
                ("assistant", "Read the docs"),  # prose, not an action
            ],
        )

    def test_old_episode_is_refolded_with_turns_kept(self):
        refolded = refold_exchanges(OLD_EPISODE, min_chars=24)
        self.assertEqual(
            refolded,
            [
                Exchange(
                    0,
                    0,
                    "User: run the suite and fix what fails\nAssistant: Running it.\n"
                    "Assistant: Fixed — the fixture leaked state.\nRead the docs before changing it again.\n"
                    "Actions: Ran 2: pytest -q; pytest -q tests/test_a.py · Edited a.py · Used TaskStop",
                ),
                Exchange(1, 0, "User: thanks, now commit it please\nActions: Ran: git commit -m fix"),
            ],
        )

    def test_refold_is_idempotent(self):
        once = refold_exchanges(OLD_EPISODE, min_chars=24)
        self.assertIsNone(refold_exchanges([(e.turn, e.part, e.text) for e in once], min_chars=24))

    def test_nothing_to_rewrite_is_none(self):
        self.assertIsNone(refold_exchanges([(0, 0, "User: hi there\nAssistant: hello, how can I help")], 24))
        self.assertIsNone(refold_exchanges([], 24))

    def test_an_episode_of_actions_alone_refolds_to_nothing(self):
        self.assertEqual(refold_exchanges([(0, 1, "Ran: ls\nRan: pwd")], 24), [])

    def test_order_of_stored_rows_does_not_matter(self):
        self.assertEqual(refold_exchanges(list(reversed(OLD_EPISODE)), 24), refold_exchanges(OLD_EPISODE, 24))


class NonFileIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = replace(get_config(), embedding="hash")
        self.store = Store(Path(self.tmp.name) / "memory.db")
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _unit(self, anchor: str, body: str, kind: str = "exchange") -> dict:
        return {
            "anchor": anchor,
            "kind": kind,
            "title": anchor,
            "heading_path": "s1",
            "level": 0,
            "summary": body[:40],
            "body": body,
            "byte_start": 0,
            "byte_end": len(body),
        }

    def test_records_are_independent_of_how_units_are_grouped(self):
        units = [self._unit("a", "deployment runs on github actions"), self._unit("b", "tailwind styles the ui")]
        together = _records_from_units(self.store, self.embedder, self.project, "s1", units)
        apart = [_records_from_units(self.store, self.embedder, self.project, "s1", [u])[0] for u in units]
        self.assertEqual(together, apart)

    def test_kinds_sharing_a_source_do_not_clobber_each_other(self):
        index_snapshot(self.store, self.embedder, self.cfg, self.project, "s1", 'heading "Login"', now=1.0)
        index_nonfile(self.store, self.embedder, self.project, "exchange", "s1", [self._unit("a", "User: hi")], now=2.0)
        kinds = sorted(r["kind"] for r in self.store.chunk_rows(self.project["key"]))
        self.assertEqual(kinds, ["exchange", "snapshot"])

    def test_reindexing_a_source_replaces_only_that_kind(self):
        index_nonfile(self.store, self.embedder, self.project, "exchange", "s1", [self._unit("a", "one")], now=1.0)
        index_nonfile(self.store, self.embedder, self.project, "exchange", "s1", [self._unit("b", "two")], now=2.0)
        rows = self.store.chunk_rows(self.project["key"], kind="exchange")
        self.assertEqual([r["anchor"] for r in rows], ["b"])

    def test_empty_units_write_nothing(self):
        self.assertEqual(index_nonfile(self.store, self.embedder, self.project, "exchange", "s1", []), [])
        self.assertEqual(self.store.chunk_rows(self.project["key"]), [])


class GateAndPrepareTests(unittest.TestCase):
    def test_gate_is_a_length_threshold(self):
        self.assertFalse(should_keep_exchange(Exchange(0, 0, "User: ok"), 24))
        self.assertTrue(should_keep_exchange(Exchange(0, 0, "User: go\nActions: Ran: pytest -q"), 24))  # footer counts

    def test_prepare_redacts_before_gating(self):
        # 52 chars raw, 22 once the secret collapses to the marker: the gate judges what is stored.
        secret_only = [("user", "token=" + "a1" * 20)]
        self.assertEqual(len(exchange_units(secret_only)[0].text), 52)
        self.assertEqual(prepare_exchanges(secret_only, "/repo", min_chars=30), [])
        self.assertEqual(prepare_exchanges(secret_only, "/repo", min_chars=0)[0].text, f"User: token={REDACTED}")

    def test_chunk_units_carry_episode_anchors(self):
        units = exchange_chunk_units(
            "sess:0", [Exchange(0, 0, "User: hi\nAssistant: hello"), Exchange(0, 1, "more")], "t"
        )
        self.assertEqual([u["anchor"] for u in units], ["sess:0:0.0", "sess:0:0.1"])
        self.assertEqual([exchange_position(u["anchor"]) for u in units], [(0, 0), (0, 1)])  # the inverse
        self.assertEqual(exchange_anchor("sess:0", 3, 2), "sess:0:3.2")
        with self.assertRaises(ValueError):
            exchange_position("installation/prerequisites")
        self.assertEqual({u["kind"] for u in units}, {"exchange"})
        self.assertEqual(units[0]["summary"], "User: hi")


if __name__ == "__main__":
    unittest.main()
