"""Episodic units + non-file chunk indexing — stdlib, hash embedder.

``exchange_units`` is the verbatim unit the LongMemEval benchmark measures and a future capture
path would store, so its grouping/splitting is pinned here. ``index_nonfile`` /
``Store.replace_nonfile_chunks`` are the one writer shared by snapshot and exchange chunks.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.domain.episodes import (  # noqa: E402
    EXCHANGE_MAX_CHARS,
    Exchange,
    exchange_units,
    prepare_exchanges,
    should_keep_exchange,
)
from core.domain.privacy import REDACTED  # noqa: E402
from core.index.indexer import _records_from_units, exchange_chunk_units, index_nonfile, index_snapshot  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402


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
        self.assertTrue(should_keep_exchange(Exchange(0, 0, "Assistant: Ran: pytest -q tests/test_x.py"), 24))

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
        self.assertEqual({u["kind"] for u in units}, {"exchange"})
        self.assertEqual(units[0]["summary"], "User: hi")


if __name__ == "__main__":
    unittest.main()
