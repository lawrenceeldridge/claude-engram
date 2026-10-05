"""LongMemEval harness tests — stdlib, hash embedder, inline fixture, no network.

The harness decides whether engram gains a verbatim layer, so its plumbing is pinned: parsing,
the dev/test hold-out, the stratified sample, each arm's unit construction and session mapping,
abstention exclusion, the hybrid fusion, dataset resolution, and the atomic download.
"""

from __future__ import annotations

import argparse
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

from bench import longmemeval as lme  # noqa: E402
from bench.cli_args import add_eval_arguments  # noqa: E402
from core.config import get_config  # noqa: E402


def _entry(qid: str, qtype: str, question: str, sessions: dict[str, list[tuple[str, str]]], gold: list[str]) -> dict:
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": question,
        "haystack_session_ids": list(sessions),
        "haystack_dates": [f"2023/05/{i + 1:02d}" for i in range(len(sessions))],
        "haystack_sessions": [[{"role": r, "content": c} for r, c in turns] for turns in sessions.values()],
        "answer_session_ids": gold,
    }


FIXTURE = [
    _entry(
        "q1",
        "single-session-user",
        "What colour is my new bicycle?",
        {
            "s-bike": [("user", "I just bought a new bicycle and it is bright green."), ("assistant", "Lovely!")],
            "s-food": [("user", "Recommend a pasta recipe with mushrooms."), ("assistant", "Try a creamy risotto.")],
            "s-work": [("user", "My manager moved our standup to Tuesdays."), ("assistant", "Noted.")],
        },
        ["s-bike"],
    ),
    _entry(
        "q2",
        "multi-session",
        "Which city did I say my sister moved to?",
        {
            "s-sis": [("user", "My sister finally moved to Lisbon last month."), ("assistant", "Exciting.")],
            "s-car": [("user", "The car needs new tyres before winter."), ("assistant", "Book a garage.")],
        },
        ["s-sis"],
    ),
    _entry("q3_abs", "single-session-user", "What is my dog's name?", {"s-x": [("user", "hello")]}, []),
]


class ParseAndSampleTests(unittest.TestCase):
    def test_parse_fields_and_abstention(self):
        qs = lme.parse(FIXTURE)
        self.assertEqual([q.qid for q in qs], ["q1", "q2", "q3_abs"])
        self.assertEqual(qs[0].gold, frozenset({"s00"}))  # "s-bike", the first haystack session
        self.assertEqual(qs[0].sessions[0].turns[0][0], "user")
        self.assertEqual([q.abstention for q in qs], [False, False, True])
        self.assertEqual([q.scoreable for q in qs], [True, True, False])

    def test_session_ids_are_neutral_so_no_unit_carries_the_answer_label(self):
        # LongMemEval ids label the evidence ("answer_…" vs "sharegpt_…"); units embed/FTS-index them
        entry = _entry("q", "t", "?", {"answer_ab12_1": [("user", "x")], "sharegpt_zz_0": [("user", "y")]}, [])
        entry["answer_session_ids"] = ["answer_ab12_1", "answer_gone_9"]
        q = lme.parse([entry])[0]
        self.assertEqual([s.sid for s in q.sessions], ["s00", "s01"])
        self.assertEqual(q.gold, frozenset({"s00", "absent:answer_gone_9"}))  # a missing gold stays a miss

    def test_stratified_sample_is_seeded_and_round_robin(self):
        qs = lme.parse(FIXTURE)
        sample = lme.stratified_sample(qs, 2, seed=1)
        self.assertEqual(len(sample), 2)
        self.assertEqual({q.qtype for q in sample}, {"multi-session", "single-session-user"})
        self.assertEqual(sample, lme.stratified_sample(qs, 2, seed=1))
        self.assertEqual(lme.stratified_sample(qs, 0), qs)  # 0 = all

    def test_session_units(self):
        s = lme.parse(FIXTURE)[0].sessions[0]
        self.assertEqual(lme.session_document(s), "I just bought a new bicycle and it is bright green.")
        self.assertIn("Lovely!", lme.session_text(s))


def _questions(per_type: dict[str, int]) -> list:
    """Bare scoreable Questions — the split only reads ids and types."""
    return [
        lme.Question(f"{qtype}-{i}", qtype, "?", (), frozenset({"s00"}))
        for qtype, n in per_type.items()
        for i in range(n)
    ]


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.questions = _questions({"multi-session": 50, "single-session-preference": 10, "temporal-reasoning": 40})

    def test_dev_and_test_are_disjoint_complete_and_stratified_by_type(self):
        dev, test = lme.select_split(self.questions, "dev"), lme.select_split(self.questions, "test")
        self.assertFalse({q.qid for q in dev} & {q.qid for q in test})
        self.assertEqual(len(dev) + len(test), len(self.questions))
        self.assertEqual(
            {
                t: sum(q.qtype == t for q in dev)
                for t in ("multi-session", "single-session-preference", "temporal-reasoning")
            },
            {"multi-session": 10, "single-session-preference": 2, "temporal-reasoning": 8},  # 20% of each type
        )

    def test_membership_is_by_question_id_not_position(self):
        dev = {q.qid for q in lme.select_split(self.questions, "dev")}
        self.assertEqual(dev, {q.qid for q in lme.select_split(list(reversed(self.questions)), "dev")})

    def test_all_is_everything_and_an_unknown_split_is_refused(self):
        self.assertEqual(lme.select_split(self.questions, "all"), self.questions)
        with self.assertRaises(ValueError):
            lme.select_split(self.questions, "train")

    def test_cli_choices_match_the_harness_and_default_to_dev(self):
        # cli_args can't import the harness (it stays stdlib-light), so pin the copy here.
        parser = argparse.ArgumentParser()
        add_eval_arguments(parser)
        action = next(a for a in parser._actions if a.dest == "lme_split")
        self.assertEqual((tuple(action.choices), action.default), (lme.SPLITS, "dev"))

    def test_run_samples_within_the_chosen_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lme.json"
            entries = [
                _entry(f"q{i}", ("multi-session", "temporal-reasoning")[i % 2], "?", {"a": [("user", "x")]}, ["a"])
                for i in range(20)
            ]
            path.write_text(json.dumps(entries))
            seen = {}
            for which in lme.SPLITS:
                args = argparse.Namespace(
                    lme_path=path,
                    lme_download=False,
                    lme_split=which,
                    lme_limit=0,
                    lme_llm=0,
                    lme_out=None,
                    lme_shipped=False,
                )
                with mock.patch.object(lme, "_report") as report, mock.patch("builtins.print"):
                    lme.run_longmemeval(get_config(), ["hash"], args)
                seen[which] = {q.qid for q in report.call_args.args[3]}
        self.assertEqual(len(seen["dev"]), 4)  # 20% of 10 per type
        self.assertEqual(seen["dev"] | seen["test"], seen["all"])
        self.assertFalse(seen["dev"] & seen["test"])


class RankingTests(unittest.TestCase):
    def test_sessions_of_keeps_first_appearance(self):
        units = [("u1", "s2", "a"), ("u2", "s1", "b"), ("u3", "s2", "c")]
        self.assertEqual(lme.sessions_of(units), ["s2", "s1"])

    def test_score_known_values(self):
        units = [("u1", "s2", "abc"), ("u2", "s1", "de")]
        row = lme.score(units, frozenset({"s1"}))
        self.assertEqual((row["any@1"], row["any@3"], row["all@5"]), (False, True, True))
        self.assertEqual(row["chars@5"], 5)

    def test_hybrid_fuses_units_from_both_arms(self):
        v = [("v1", "s1", "x"), ("v2", "s2", "y")]
        d = [("d1", "s2", "z")]
        fused = lme.rank_hybrid(v, d)
        self.assertEqual({u[0] for u in fused}, {"v1", "v2", "d1"})
        self.assertEqual(len(fused), 3)


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        # Hermetic whatever the developer's env (ENGRAM_DISTILLER=claude is common): the heuristic
        # distiller, and a hard guard — an LLM distiller fails open to the heuristic, so a stray call
        # would otherwise pass silently while spending external API calls.
        self.cfg = replace(get_config(), distiller="heuristic")
        llm = mock.patch("core.adapters.llm_distillers.subprocess.run")
        self.llm_call = llm.start()
        self.addCleanup(llm.stop)
        self.addCleanup(lambda: self.assertFalse(self.llm_call.called, "a harness test reached an LLM distiller"))

    def tearDown(self):
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_all_arms_run_and_abstention_is_excluded(self):
        result = lme.evaluate_longmemeval("hash", lme.parse(FIXTURE), self.cfg)
        self.assertEqual((result["scored"], result["abstention"]), (2, 1))
        self.assertEqual([r["arm"] for r in result["summary"]], list(lme.ARMS))
        self.assertTrue(all(r["n"] == 2 for r in result["summary"]))
        parity = next(r for r in result["summary"] if r["arm"] == "P")
        self.assertEqual(parity["R_any@1"], 1.0)  # lexically clear fixture: the answer session ranks first
        self.assertEqual([r["comparison"] for r in result["paired"]][0], "D -> V")
        self.assertEqual(set(result["paired"][0]), set(lme.PAIRED_COLS))
        self.assertEqual([r["qid"] for r in result["records"]], ["q1", "q2"])
        self.assertEqual(set(result["records"][0]["arms"]), set(lme.ARMS))

    def test_verbatim_arm_indexes_exchanges_and_maps_to_sessions(self):
        q = lme.parse(FIXTURE)[0]
        cfg = replace(self.cfg, episodic_min_chars=0)  # mapping, not the gate, is under test here
        units = lme.rank_verbatim(lme.make_embedder("hash", None, 0, cfg), cfg, q, Path(self.tmp.name))
        self.assertTrue(units)
        self.assertTrue(all(sid in {"s00", "s01", "s02"} for _u, sid, _t in units))
        self.assertTrue(all(text.startswith("User:") for _u, _s, text in units))

    def test_shipped_arms_are_opt_in_and_paired_with_their_direct_arm(self):
        default = lme.evaluate_longmemeval("hash", lme.parse(FIXTURE), self.cfg)
        self.assertEqual(default["arms"], lme.ARMS)
        shipped = lme.evaluate_longmemeval("hash", lme.parse(FIXTURE), self.cfg, shipped=True)
        self.assertEqual([r["arm"] for r in shipped["summary"]], [*lme.ARMS, *lme.SHIPPED_ARMS])
        self.assertIn("D -> Ds", [r["comparison"] for r in shipped["paired"]])

    def test_shipped_path_captures_the_transcript_and_maps_both_surfaces_by_episode(self):
        q = lme.parse(FIXTURE)[0]
        ranked = lme.rank_shipped(lme.make_embedder("hash", None, 0, self.cfg), self.cfg, q, Path(self.tmp.name))
        sids = {s.sid for s in q.sessions}
        self.assertTrue(ranked["Vs"] and ranked["Ds"])
        self.assertTrue({sid for _u, sid, _t in ranked["Vs"]} <= sids)  # every exchange maps to its session
        self.assertTrue({sid for _u, sid, _t in ranked["Ds"]} <= sids)  # every fact is linked to its episode
        self.assertEqual(lme.sessions_of(ranked["Vs"])[0], "s00")  # the bicycle session
        self.assertEqual(len(ranked["Hs"]), len({u[0] for u in ranked["Vs"] + ranked["Ds"]}))

    def test_session_transcript_round_trips_through_the_capture_parser(self):
        from core.transcript import extract_incremental_parts

        session = lme.parse(FIXTURE)[0].sessions[0]
        path = Path(self.tmp.name) / "t.jsonl"
        path.write_text(lme.session_transcript(session), encoding="utf-8")
        self.assertEqual(tuple(extract_incremental_parts(str(path), 0).turns), session.turns)

    def test_each_record_reaches_progress_as_its_question_finishes(self):
        seen = []
        result = lme.evaluate_longmemeval(
            "hash", lme.parse(FIXTURE), self.cfg, lambda done, total, rec: seen.append((done, total, rec["qid"]))
        )
        self.assertEqual(seen, [(1, 2, "q1"), (2, 2, "q2")])
        self.assertEqual([r["qid"] for r in result["records"]], ["q1", "q2"])

    def test_report_appends_records_to_lme_out_incrementally(self):
        out = Path(self.tmp.name) / "records.jsonl"
        subset = [q for q in lme.parse(FIXTURE) if q.scoreable]
        with mock.patch("builtins.print"):
            lme._report(["hash"], "heuristic", self.cfg, subset, out, shipped=False)
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        self.assertEqual(
            [(r["backend"], r["distiller"], r["qid"]) for r in rows],
            [("hash", "heuristic", "q1"), ("hash", "heuristic", "q2")],
        )

    def test_plus_float_is_rejected(self):
        with self.assertRaises(ValueError):
            lme.evaluate_longmemeval("hash+float", [], self.cfg)


class DatasetTests(unittest.TestCase):
    def test_resolve_prefers_explicit_path_then_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = mock.Mock(data_dir=tmp)
            args = argparse.Namespace(lme_path=None, lme_download=False)
            self.assertIsNone(lme.resolve_dataset(args, cfg))
            cached = Path(tmp) / "bench-cache" / lme.LME_FILE
            cached.parent.mkdir(parents=True)
            cached.write_text("[]")
            self.assertEqual(lme.resolve_dataset(args, cfg), cached)
            explicit = Path(tmp) / "other.json"
            self.assertEqual(
                lme.resolve_dataset(argparse.Namespace(lme_path=explicit, lme_download=False), cfg), explicit
            )

    def test_download_is_atomic_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:

            def fake_fetch(url, dest):
                Path(dest).write_text(json.dumps(FIXTURE))

            with mock.patch.object(lme.urllib.request, "urlretrieve", side_effect=fake_fetch) as fetch:
                path = lme.download(Path(tmp))
                again = lme.download(Path(tmp))
            self.assertEqual(path, again)
            self.assertEqual(fetch.call_count, 1)
            self.assertFalse(path.with_suffix(".part").exists())
            questions, digest = lme.load(path)
            self.assertEqual((len(questions), len(digest)), (3, 64))


class DistillerSafetyTests(unittest.TestCase):
    """The D arm must never inherit an LLM distiller from ENGRAM_DISTILLER — one external call per
    session, unapproved (it happened once in development). LLM runs are explicit opt-in only."""

    def _runs(self, configured: str, llm_limit: int, sample=("q1", "q2", "q3")):
        runs = lme.distiller_runs(replace(get_config(), distiller=configured), list(sample), llm_limit)
        return [(label, run_cfg.distiller, subset) for label, run_cfg, subset in runs]

    def test_default_runs_only_the_heuristic_even_when_an_llm_is_configured(self):
        self.assertEqual(self._runs("claude", 0), [("heuristic", "heuristic", ["q1", "q2", "q3"])])

    def test_llm_run_is_opt_in_uses_the_configured_llm_and_is_capped(self):
        self.assertEqual(self._runs("ollama", 2)[1], ("ollama", "ollama", ["q1", "q2"]))

    def test_llm_run_falls_back_to_claude_when_no_llm_is_configured(self):
        self.assertEqual(self._runs("heuristic", 1)[1][:2], ("claude", "claude"))


if __name__ == "__main__":
    unittest.main()
