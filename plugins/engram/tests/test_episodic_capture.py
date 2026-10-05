"""Episodic capture — verbatim exchanges indexed beside the distilled facts, ADDITIVELY.

The load-bearing test is parity: the same transcript yields byte-identical ``facts`` text whether
the episodic layer is on or off. Around it: exchanges are stored redacted, keyed by episode,
idempotent on re-capture, never enter the facts recall surface, fail open, are forgotten by
consolidation, and read as fresh through both index read paths. Then the surfaces: facts link to
their episode only when it was stored, recall carries the link, consolidation unlinks a forgotten
episode, and ``search_history`` finds exchanges / snapshots and scopes to one episode. Last, the
one-off rewrite of exchanges stored before actions were folded into a footer: migration ``_v20``
publishes it once per old episode, and the ``exchange_format`` handler keeps every identity.
Stdlib unittest, heuristic distiller, hash embedder, no network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.consolidation import consolidate  # noqa: E402
from core.domain.episodes import Exchange  # noqa: E402
from core.domain.privacy import REDACTED  # noqa: E402
from core.index.index_recall import get_chunk, search_index  # noqa: E402
from core.index.indexer import exchange_chunk_units, index_nonfile, index_snapshot  # noqa: E402
from core.ports.distill import DistilledFact  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.ports.workqueue import EXCHANGE_FORMAT  # noqa: E402
from core.store import Store  # noqa: E402


def _turn(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}) + "\n"


_TRANSCRIPT = _turn(
    "user", "where does the app deploy, and which key does CI use? I set API_KEY=abc123secretvalue."
) + _turn("assistant", "The deploy target is fly.io. The database is Postgres and CI runs on GitHub Actions.")


class _TranscriptCase(unittest.TestCase):
    """One captured two-turn transcript (with a secret in it) per test, into a fresh store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        self.tf.write(_TRANSCRIPT)
        self.tf.flush()
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        os.unlink(self.tf.name)
        self.tmp.cleanup()

    def _cfg(self, **overrides):
        return replace(get_config(), distiller="heuristic", embedding="hash", **overrides)

    def _capture(self, episodic_enabled: bool, db_name: str | None = None):
        store = Store(Path(self.tmp.name) / (db_name or f"mem-{episodic_enabled}.db"))
        cfg = self._cfg(episodic_enabled=episodic_enabled)
        service.capture_transcript_incremental(
            store, HashEmbedding(dim=cfg.dim), cfg, self.project, "sess-1", self.tf.name
        )
        return store, cfg

    def _facts(self, store) -> list[str]:
        return sorted(r["text"] for r in store.db.execute("SELECT text FROM facts WHERE project_key = ?", ("proj",)))


class EpisodicCaptureTests(_TranscriptCase):
    def test_facts_are_byte_identical_with_episodic_on_or_off(self):
        on, _ = self._capture(True)
        off, _ = self._capture(False)
        try:
            self.assertEqual(self._facts(on), self._facts(off))  # PARITY
            self.assertTrue(self._facts(on))
        finally:
            on.close()
            off.close()

    def test_exchange_is_stored_redacted_and_keyed_by_episode(self):
        store, _ = self._capture(True)
        try:
            rows = store.chunk_rows("proj", kind="exchange")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_path"], "sess-1:0")  # episode = <session>:<delta start>
            self.assertEqual(rows[0]["anchor"], "sess-1:0:0.0")
            self.assertIn("fly.io", rows[0]["body"])
            self.assertIn(f"API_KEY={REDACTED}", rows[0]["body"])
            self.assertNotIn("abc123secretvalue", rows[0]["body"])
        finally:
            store.close()

    def test_disabled_stores_no_exchanges(self):
        store, _ = self._capture(False)
        try:
            self.assertEqual(store.chunk_rows("proj", kind="exchange"), [])
        finally:
            store.close()

    def test_recapturing_the_same_delta_replaces_not_duplicates(self):
        store, cfg = self._capture(True, db_name="twice.db")
        try:
            delta = service.extract_incremental_parts(self.tf.name, 0)
            service.capture_episodes(store, HashEmbedding(dim=cfg.dim), cfg, self.project, "sess-1", delta)
            self.assertEqual(len(store.chunk_rows("proj", kind="exchange")), 1)
        finally:
            store.close()

    def test_exchanges_never_enter_the_facts_recall_surface(self):
        store, cfg = self._capture(True)
        try:
            hits = service.recall_structured(store, HashEmbedding(dim=cfg.dim), cfg, self.project, "fly.io deploy")
            self.assertTrue(all(f["kind"] != "exchange" for f in hits["facts"]))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM facts WHERE kind = 'exchange'").fetchone()[0], 0)
        finally:
            store.close()

    def test_a_broken_episodic_write_fails_open(self):
        with mock.patch.object(service, "capture_episodes", side_effect=RuntimeError("index down")):
            store, _ = self._capture(True, db_name="broken.db")
        try:
            self.assertTrue(self._facts(store))  # facts still captured
            cursor = store.get_capture_cursor("proj:sess-1")
            self.assertEqual(cursor, os.path.getsize(self.tf.name))  # cursor still advanced
        finally:
            store.close()

    def _kept_by_default_gate(self, turns: list[tuple[str, str]]) -> int:
        store = Store(Path(self.tmp.name) / "gate.db")
        cfg = self._cfg()  # the shipped episodic_min_chars
        delta = service.TranscriptDelta("", [], turns, 0, 10)
        try:
            return service.capture_episodes(store, HashEmbedding(dim=cfg.dim), cfg, self.project, "s", delta)
        finally:
            store.close()

    def test_short_trivia_is_not_kept(self):
        # the gate counts role labels, so it drops one-line trivia; "User: ok\nAssistant: done" (24) is kept
        self.assertEqual(self._kept_by_default_gate([("user", "thanks")]), 0)

    def test_a_short_exchange_that_carries_an_answer_is_kept(self):
        # 75 chars — why the default gate is 24, not 80 (tracker: gate A/B, 2026-09-25)
        turns = [("user", "I got a new bicycle, it's bright green."), ("assistant", "Lovely!")]
        self.assertEqual(self._kept_by_default_gate(turns), 1)


class EpisodeProvenanceTests(_TranscriptCase):
    """Facts ↔ episode: the link is written only when the episode's exchanges exist."""

    def _episodes(self, store) -> dict[str, str | None]:
        rows = store.db.execute("SELECT kind, episode FROM facts WHERE project_key = ?", ("proj",))
        return {r["kind"]: r["episode"] for r in rows}

    def test_facts_and_prompts_link_to_their_stored_episode(self):
        store, _ = self._capture(True)
        try:
            self.assertEqual(self._episodes(store), {"fact": "sess-1:0", "prompt": "sess-1:0"})
            self.assertEqual({r["source_path"] for r in store.chunk_rows("proj", kind="exchange")}, {"sess-1:0"})
        finally:
            store.close()

    def test_no_link_when_episodic_is_off_or_fails(self):
        off, _ = self._capture(False)
        with mock.patch.object(service, "capture_episodes", side_effect=RuntimeError("index down")):
            broken, _ = self._capture(True, db_name="broken.db")
        try:
            for store in (off, broken):
                self.assertEqual(set(self._episodes(store).values()), {None})
        finally:
            off.close()
            broken.close()

    def test_no_link_when_nothing_substantive_was_kept(self):
        with mock.patch.object(service, "capture_episodes", return_value=0):
            store, _ = self._capture(True, db_name="trivia.db")
        try:
            self.assertEqual(set(self._episodes(store).values()), {None})
        finally:
            store.close()

    def test_recall_carries_the_link_only_when_there_is_one(self):
        on, cfg = self._capture(True)
        off, _ = self._capture(False)
        try:
            for store, expected in ((on, "sess-1:0"), (off, None)):
                facts = service.recall_structured(store, HashEmbedding(dim=cfg.dim), cfg, self.project, "fly.io")[
                    "facts"
                ]
                self.assertTrue(facts)
                self.assertEqual({f.get("episode") for f in facts}, {expected})
                if expected is None:
                    self.assertTrue(all("episode" not in f for f in facts))  # omitted, not null
        finally:
            on.close()
            off.close()

    def test_a_distiller_transcript_stores_nothing_but_advances(self):
        tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        tf.write(_turn("user", "You extract durable long-term memory from a session. The deploy target is fly.io."))
        tf.close()
        store = Store(Path(self.tmp.name) / "nested.db")
        cfg = self._cfg(episodic_enabled=True, sensory_enabled=True)
        try:
            service.capture_transcript_incremental(store, HashEmbedding(dim=cfg.dim), cfg, self.project, "n", tf.name)
            self.assertEqual(self._facts(store), [])
            self.assertEqual(store.chunk_rows("proj", kind="exchange"), [])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM sensory").fetchone()[0], 0)
            self.assertEqual(store.get_capture_cursor("proj:n"), os.path.getsize(tf.name))
        finally:
            store.close()
            os.unlink(tf.name)


class EpisodeLinkStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.db"
        self.store = Store(self.path)
        self.cfg = replace(get_config(), embedding="hash", episodic_min_chars=0)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fact(self, text: str, episode: str | None) -> str:
        service.add_records(
            self.store, self.embedder, self.cfg, self.project, "s", [DistilledFact(text)], episode=episode
        )
        return self.store.fact_id("proj", text)

    def test_reinforcing_repoints_to_the_newest_episode_and_none_keeps_it(self):
        fid = self._fact("deploys go to fly.io", "s:0")
        self._fact("deploys go to fly.io", "s:900")
        self.assertEqual(self.store.get(fid)["episode"], "s:900")
        self._fact("deploys go to fly.io", None)
        self.assertEqual(self.store.get(fid)["episode"], "s:900")

    def test_consolidation_unlinks_facts_whose_episode_was_forgotten(self):
        day = 86400.0
        for session, age in (("old", 400), ("new", 1)):
            delta = service.TranscriptDelta("", [], [("user", f"{session} talk about deploys")], 0, 1)
            service.capture_episodes(
                self.store, self.embedder, self.cfg, self.project, session, delta, now=1000 * day - age * day
            )
        stale, kept = self._fact("old fact", "old:0"), self._fact("new fact", "new:0")
        consolidate(self.store, replace(self.cfg, episodic_ttl_days=180), self.project, now=1000 * day)
        self.assertIsNone(self.store.get(stale)["episode"])
        self.assertEqual(self.store.get(kept)["episode"], "new:0")

    def test_v19_heals_a_database_stamped_before_the_column(self):
        self.store.db.execute("ALTER TABLE facts DROP COLUMN episode")
        self.store.db.execute("PRAGMA user_version = 18")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.assertIn("episode", {r[1] for r in self.store.db.execute("PRAGMA table_info(facts)")})


_OLD_FORMAT = [  # one exchange as stored before the footer: its tool run spilled into a part of its own
    Exchange(0, 0, "User: run the suite and fix what fails\nAssistant: Running it now.\nRan: pytest -q"),
    Exchange(0, 1, "Ran: pytest -q tests/test_a.py\nEdited a.py\nUsed TaskStop: {'task_id': 'b4'}"),
    Exchange(0, 2, "Assistant: Fixed — the fixture leaked state between tests."),
]


class ExchangeFormatRewriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.db"
        self.store = Store(self.path)
        self.cfg = replace(get_config(), embedding="hash", distiller="heuristic")
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _store_exchanges(self, episode: str, exchanges: list[Exchange], stamp: float = 5000.0) -> None:
        units = exchange_chunk_units(episode, exchanges, "2026-09-20 10:00")
        index_nonfile(self.store, self.embedder, self.project, "exchange", episode, units, now=stamp)

    def _replay_migrations(self) -> int:
        """Reopen as a database stamped one step below head, so the ladder replays (as on upgrade)."""
        self.store.db.execute("PRAGMA user_version = 19")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.path)
        return self.store.count_work(stage=EXCHANGE_FORMAT)

    def _bodies(self, episode: str) -> dict[str, str]:
        return {r["anchor"]: r["body"] for r in self.store.chunk_rows("proj", "exchange", episode)}

    def test_migration_publishes_one_command_per_old_format_episode(self):
        self._store_exchanges("s:0", _OLD_FORMAT)
        current = [("user", "deploy it please"), ("assistant", "Deploying now."), ("action", "Ran: fly deploy")]
        service.capture_episodes(
            self.store, self.embedder, self.cfg, self.project, "s", service.TranscriptDelta("", [], current, 900, 901)
        )
        self._store_exchanges("s:1800", [Exchange(0, 0, "User: what is the deploy target?\nAssistant: fly.io")])
        self.assertEqual(self._replay_migrations(), 1)  # only the old-format episode
        (item,) = self.store.work_items("proj")
        self.assertEqual((item["stage"], item["ref"]), (EXCHANGE_FORMAT, "s:0"))
        self.assertEqual(self._replay_migrations(), 1)  # republishing is idempotent on msg_id

    def test_the_rewrite_refolds_and_keeps_episode_title_and_age(self):
        self._store_exchanges("s:0", _OLD_FORMAT, stamp=5000.0)
        fact = self.store.fact_id("proj", "the fixture leaked state")
        service.add_records(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s",
            [DistilledFact("the fixture leaked state")],
            episode="s:0",
        )
        self._replay_migrations()
        self.assertEqual(service.reformat_exchanges(self.store, self.embedder, self.cfg), 1)
        rows = self.store.chunk_rows("proj", "exchange", "s:0")
        self.assertEqual(
            [r["body"] for r in rows],
            [
                "User: run the suite and fix what fails\nAssistant: Running it now.\n"
                "Assistant: Fixed — the fixture leaked state between tests.\n"
                "Actions: Ran 2: pytest -q; pytest -q tests/test_a.py · Edited a.py · Used TaskStop"
            ],
        )
        self.assertEqual([r["anchor"] for r in rows], ["s:0:0.0"])
        self.assertEqual({(r["title"], r["indexed_at"]) for r in rows}, {("2026-09-20 10:00", 5000.0)})
        self.assertEqual(self.store.get(fact)["episode"], "s:0")  # the link holds
        self.assertEqual(self.store.count_work(stage=EXCHANGE_FORMAT), 0)  # acked
        self.assertEqual(self._replay_migrations(), 0)  # self-limiting: nothing old is left to publish

    def test_an_episode_left_with_no_conversation_is_removed_and_unlinked(self):
        self._store_exchanges("s:0", [Exchange(0, 1, "Ran: ls\nRan: pwd\nEdited a.py")])
        fact = self.store.fact_id("proj", "listed the repo")
        service.add_records(
            self.store, self.embedder, self.cfg, self.project, "s", [DistilledFact("listed the repo")], episode="s:0"
        )
        self._replay_migrations()
        service.reformat_exchanges(self.store, self.embedder, self.cfg)
        self.assertEqual(self._bodies("s:0"), {})
        self.assertIsNone(self.store.get(fact)["episode"])

    def test_a_current_or_forgotten_episode_is_acked_untouched(self):
        self._store_exchanges("s:1800", [Exchange(0, 0, "User: what is the deploy target?\nAssistant: fly.io")])
        before = self._bodies("s:1800")
        for episode in ("s:1800", "gone:0"):
            self.store.enqueue_work(msg_id=f"x:{episode}", stage=EXCHANGE_FORMAT, project_key="proj", ref=episode)
        self.assertEqual(service.reformat_exchanges(self.store, self.embedder, self.cfg), 0)
        self.assertEqual(self._bodies("s:1800"), before)
        self.assertEqual(self.store.count_work(stage=EXCHANGE_FORMAT), 0)

    def test_a_malformed_item_is_dead_lettered_not_retried(self):
        units = exchange_chunk_units("bad:0", [Exchange(0, 0, "User: hello there\nRan: ls")], "t")
        units[0]["anchor"] = "not-an-exchange-anchor"
        index_nonfile(self.store, self.embedder, self.project, "exchange", "bad:0", units)
        self.store.enqueue_work(msg_id="x:bad", stage=EXCHANGE_FORMAT, project_key="proj", ref="bad:0")
        self.assertEqual(service.reformat_exchanges(self.store, self.embedder, self.cfg), 0)
        self.assertEqual(self.store.count_work(stage=EXCHANGE_FORMAT, status="dead"), 1)

    def test_a_failing_rewrite_is_retried_later(self):
        self._store_exchanges("s:0", _OLD_FORMAT)
        self._replay_migrations()
        with mock.patch("core.index.indexer.index_nonfile", side_effect=RuntimeError("embedder down")):
            self.assertEqual(service.reformat_exchanges(self.store, self.embedder, self.cfg), 0)
        self.assertEqual(self.store.count_work(stage=EXCHANGE_FORMAT, status="pending"), 1)  # nak'd, not lost
        self.assertEqual(len(self._bodies("s:0")), 3)  # untouched

    def test_capture_drains_the_rewrite_queue(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as tf:
            tf.write(_TRANSCRIPT)
        try:
            with mock.patch.object(service, "reformat_exchanges") as drain:
                service.capture_transcript_incremental(
                    self.store, self.embedder, self.cfg, self.project, "sess-9", tf.name
                )
            drain.assert_called_once()
        finally:
            os.unlink(tf.name)


class HistorySearchTests(unittest.TestCase):
    """``search_index`` scoped to one source, and the ``search_history`` MCP tool over it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        os.environ["ENGRAM_EMBEDDING"] = "hash"
        import mcp_server

        self.mcp = mcp_server
        self.mcp.ENGINE = mcp_server._Engine()
        self.mcp.ENGINE._init()
        engine = self.mcp.ENGINE
        self.store, self.embedder, self.project = engine.store, engine.embedder, engine._project(None)
        self.cfg = replace(engine.cfg, episodic_min_chars=0)
        for session in ("a", "b"):
            turns = [("user", f"deploy question {i} in session {session}") for i in range(5)]
            delta = service.TranscriptDelta("", [], turns, 0, 1)
            service.capture_episodes(self.store, self.embedder, self.cfg, self.project, session, delta)

    def tearDown(self):
        for key in ("ENGRAM_DATA_DIR", "ENGRAM_EMBEDDING"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _call(self, name: str, arguments: dict) -> dict:
        resp = self.mcp._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )
        return json.loads(resp["result"]["content"][0]["text"])

    def test_unscoped_search_caps_each_episode_scoped_search_does_not(self):
        wide = search_index(self.store, self.embedder, self.cfg, self.project, "deploy question", kind="exchange")
        self.assertEqual({r["source_path"] for r in wide["results"]}, {"a:0", "b:0"})
        self.assertLessEqual(max(sum(r["source_path"] == s for r in wide["results"]) for s in ("a:0", "b:0")), 3)
        scoped = search_index(
            self.store, self.embedder, self.cfg, self.project, "deploy question", kind="exchange", source_path="a:0"
        )
        self.assertEqual([r["source_path"] for r in scoped["results"]], ["a:0"] * 5)

    def test_search_history_defaults_to_exchanges_and_scopes_to_an_episode(self):
        found = self._call("search_history", {"query": "deploy question", "episode": "b:0"})
        self.assertEqual({(r["kind"], r["source_path"]) for r in found["results"]}, {("exchange", "b:0")})
        body = self._call("get_doc_section", {"ref": found["results"][0]["anchor"]})
        self.assertTrue(body["found"])
        self.assertIn("session b", body["body"])

    def test_search_history_finds_snapshots(self):
        import time

        index_snapshot(
            self.store, self.embedder, self.cfg, self.project, "https://x/app", 'heading "Deploy"', now=time.time()
        )
        found = self._call("search_history", {"query": "deploy", "kind": "snapshots"})
        self.assertEqual({r["kind"] for r in found["results"]}, {"snapshot"})

    def test_search_history_rejects_an_unknown_kind(self):
        self.assertIn("error", self._call("search_history", {"query": "x", "kind": "code"}))

    def test_a_history_pull_books_no_file_saving(self):
        anchor = self._call("search_history", {"query": "deploy question"})["results"][0]["anchor"]
        with mock.patch.object(self.store, "record_usage") as record:
            self._call("get_doc_section", {"ref": anchor})
        record.assert_not_called()


class EpisodicIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = replace(get_config(), embedding="hash", episodic_min_chars=0)
        self.store = Store(Path(self.tmp.name) / "memory.db")
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _episode(self, session: str, text: str, now: float) -> None:
        delta = service.TranscriptDelta("", [], [("user", text)], 0, 1)
        service.capture_episodes(self.store, self.embedder, self.cfg, self.project, session, delta, now=now)

    def test_exchanges_read_fresh_through_search_and_get_chunk(self):
        self._episode("s1", "we chose fly.io for deploys", now=1.0)
        found = search_index(self.store, self.embedder, self.cfg, self.project, "fly.io deploys", kind="exchange")
        self.assertEqual([r["freshness"] for r in found["results"]], ["fresh"])
        self.assertEqual(get_chunk(self.store, self.project, found["results"][0]["anchor"])["freshness"], "fresh")

    def test_get_chunk_on_a_snapshot_uses_its_age_rule_not_file_drift(self):
        import time

        index_snapshot(
            self.store, self.embedder, self.cfg, self.project, "https://x/app", 'heading "Login"', now=time.time()
        )
        self.assertEqual(get_chunk(self.store, self.project, "snapshot")["freshness"], "fresh")  # was "gone"

    def test_consolidation_forgets_old_and_over_cap_exchanges(self):
        day = 86400.0
        for i, age in enumerate((400, 200, 10, 5, 1)):
            self._episode(f"s{i}", f"exchange number {i} about deploy pipelines", now=1000 * day - age * day)
        cfg = replace(self.cfg, episodic_ttl_days=180, episodic_max_chunks=2)
        counts = consolidate(self.store, cfg, self.project, now=1000 * day)
        self.assertEqual(counts["forgotten"], 3)  # 2 past the horizon, then 1 beyond the cap
        kept = sorted(r["source_path"] for r in self.store.chunk_rows("proj", kind="exchange"))
        self.assertEqual(kept, ["s3:0", "s4:0"])  # the two newest survive

    def test_chunk_stats_counts_and_sizes(self):
        self._episode("s1", "alpha beta gamma", now=1.0)
        stats = self.store.chunk_stats("proj", "exchange")
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["bytes"], len("User: alpha beta gamma"))

    def test_zero_limits_forget_nothing(self):
        self._episode("s1", "kept forever", now=1.0)
        self.assertEqual(
            self.store.prune_nonfile_chunks("proj", "exchange", max_age_seconds=0, keep_max=0, now=10**9), 0
        )


if __name__ == "__main__":
    unittest.main()
