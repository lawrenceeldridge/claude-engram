"""Anti-pattern catalogue (Phase 1).

The capture worker mines admitted mistakes into durable ``kind="antipattern"`` memories
(a strict rule + do/don't), scope-routed to the project or the reserved global key, and
surfaced by ordinary recall. Verifies the pure pieces (admission gate, prompt parser, text
cap), the re-entrancy backstop, the service command (routing, additive dedup, LTM tier,
existing-fed-to-prompt), the gate/throttle wrapper, and the global recall union.

Stdlib unittest, hash embedder, a stub distiller — no live LLM, no network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

import prefer_memory  # noqa: E402  (PreToolUse guard — Phase 3 anti-pattern warning)

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.consolidation import consolidate  # noqa: E402
from core.consolidation.invalidate import invalidate_stale_antipatterns  # noqa: E402
from core.consolidation.refine import refine  # noqa: E402
from core.ports.distill import (  # noqa: E402
    DistilledFact,
    HeuristicDistiller,
    _antipattern_text,
    has_admission_markers,
    is_distiller_prompt,
    parse_antipatterns,
)
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.project import GLOBAL_PROJECT_KEY, global_project  # noqa: E402
from core.recall import _recall_rows, search_fused  # noqa: E402
from core.store import Store  # noqa: E402

_CURL = {
    "title": "Bash Tool Parameter Injection",
    "scope": "global",
    "anti_pattern": "Appended an AI-tool flag into a shell command string",
    "root_cause": "Conflated the tool wrapper's params with curl syntax",
    "strict_rule": "Never put AI-tool flags in a shell command string; set them in the tool schema",
    "dont": "curl -X PUT https://api/data --sandbox disabled",
    "do": "set sandbox:false in the tool call; then curl -X PUT https://api/data",
    "supersedes": [],
}


class AdmissionGateTests(unittest.TestCase):
    def test_positive_markers(self):
        for text in (
            "Actually I mistakenly used the wrong flag.",
            "That was wrong — let me fix that.",
            "No, don't do it that way.",
            "My apologies, I made an error there.",
            # the assistant's own corrective phrasing (previously slipped the gate)
            "My perl edit corrupted the LaTeX; let me revert.",
            "I misread the build output as clean.",
            "That was a self-inflicted bug; I broke the alias.",
            "You're right, that was my error.",
            "I forgot to update the manifest; scratch that.",
        ):
            self.assertTrue(has_admission_markers(text), text)

    def test_negative_markers(self):
        for text in (
            "Added a new feature and wrote tests; all green.",
            "The deploy target is fly.io.",
            "Refactored the recall path for clarity.",
        ):
            self.assertFalse(has_admission_markers(text), text)

    def test_case_insensitive(self):
        self.assertTrue(has_admission_markers("I MISTAKENLY shipped it"))


class ParseAntipatternsTests(unittest.TestCase):
    def test_parses_full_entry(self):
        recs = parse_antipatterns(json.dumps({"antipatterns": [_CURL]}))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r.type, "antipattern")
        self.assertEqual(r.scope, "global")
        self.assertEqual(r.title, "Bash Tool Parameter Injection")
        self.assertEqual(r.subtitle, _CURL["anti_pattern"])
        # text is the rule ALONE, whole; DON'T/DO/root-cause live in the narrative (shown once).
        self.assertEqual(r.text, _CURL["strict_rule"])
        self.assertNotIn("DON'T", r.text)
        self.assertIn("Root cause:", r.narrative)
        self.assertIn("DON'T: curl -X PUT", r.narrative)
        self.assertIn("DO: set sandbox:false", r.narrative)

    def test_rule_stored_whole_no_ellipsis(self):
        # A normal rule is well under the guard, so it is stored whole — no ellipsis, no mid-word cut.
        r = parse_antipatterns(json.dumps({"antipatterns": [_CURL]}))[0]
        self.assertEqual(r.text, _CURL["strict_rule"])
        self.assertFalse(r.text.endswith("…"))

    def test_scope_defaults_to_project(self):
        item = {**_CURL}
        del item["scope"]
        self.assertEqual(parse_antipatterns(json.dumps({"antipatterns": [item]}))[0].scope, "project")
        # An unknown scope value is also treated as project (conservative).
        weird = {**_CURL, "scope": "team"}
        self.assertEqual(parse_antipatterns(json.dumps({"antipatterns": [weird]}))[0].scope, "project")

    def test_drops_entry_without_strict_rule(self):
        self.assertEqual(parse_antipatterns(json.dumps({"antipatterns": [{"dont": "x"}]})), [])

    def test_empty_list(self):
        self.assertEqual(parse_antipatterns(json.dumps({"antipatterns": []})), [])

    def test_supersedes_filtered(self):
        item = {**_CURL, "supersedes": ["abc123", "none", ""]}
        self.assertEqual(parse_antipatterns(json.dumps({"antipatterns": [item]}))[0].supersedes, ["abc123"])


class TextCapTests(unittest.TestCase):
    def test_text_is_the_rule_alone(self):
        # DON'T/DO are NOT bundled into text — it is just the rule (trailing period stripped).
        text = _antipattern_text("Never do X.")
        self.assertEqual(text, "Never do X")

    def test_runaway_guard_trims_a_long_rule_on_word_boundary(self):
        # A pathologically long rule: the guard trims to <= cap, ends with an ellipsis,
        # and cuts on a word boundary (never mid-word).
        words = "alpha beta gamma delta epsilon zeta eta theta".split()
        text = _antipattern_text("Rule " + " ".join(words * 10), cap=60)
        self.assertLessEqual(len(text), 60)
        self.assertTrue(text.startswith("Rule alpha"))
        self.assertTrue(text.endswith("…"))
        body = text[:-1].rstrip()  # drop the ellipsis
        self.assertIn(body.split()[-1], set(words) | {"Rule"})  # last token is whole


class BackstopTests(unittest.TestCase):
    def test_antipattern_prompt_recognised(self):
        # The extraction prompt must be dropped if it is ever captured as a transcript.
        self.assertTrue(is_distiller_prompt("You review a coding-assistant session for MISTAKES"))

    def test_heuristic_returns_nothing(self):
        self.assertEqual(HeuristicDistiller().extract_antipatterns("I mistakenly did X", []), [])


class _StubDistiller:
    """Records calls and returns pre-set anti-pattern DistilledFacts."""

    def __init__(self, records):
        self._records = records
        self.calls: list[tuple[str, list]] = []

    def extract_antipatterns(self, text, existing):
        self.calls.append((text, list(existing)))
        return [replace(r) for r in self._records]


class CaptureAntipatternsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "proj1", "path": "/tmp/proj1", "label": "proj1"}
        self._saved_get_distiller = service.get_distiller
        self._saved_extract_text = service.extract_text

    def tearDown(self):
        service.get_distiller = self._saved_get_distiller
        service.extract_text = self._saved_extract_text
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _patch(self, records, text="I mistakenly used the wrong flag; let me fix that."):
        stub = _StubDistiller(records)
        service.get_distiller = lambda cfg: stub
        service.extract_text = lambda path: text
        return stub

    def _rec(self, **kw):
        base = dict(text="Never put tool flags in shell commands", type="antipattern", scope="project")
        base.update(kw)
        return DistilledFact(**base)

    def test_project_scoped_stored_in_ltm(self):
        self._patch([self._rec()])
        n = service.capture_antipatterns(self.store, self.embedder, self.cfg, self.project, "s1", "/t")
        self.assertEqual(n, 1)
        rows = self.store.active_antipatterns(self.project["key"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "antipattern")
        self.assertEqual(rows[0]["tier"], "ltm")
        self.assertEqual(self.store.active_antipatterns(GLOBAL_PROJECT_KEY), [])

    def test_global_scoped_routed_to_global_key(self):
        self._patch([self._rec(text="Never inject a tool flag into curl", scope="global")])
        service.capture_antipatterns(self.store, self.embedder, self.cfg, self.project, "s1", "/t")
        self.assertEqual(len(self.store.active_antipatterns(GLOBAL_PROJECT_KEY)), 1)
        self.assertEqual(self.store.active_antipatterns(self.project["key"]), [])

    def test_additive_dedup_no_duplicate_on_rerun(self):
        self._patch([self._rec()])
        service.capture_antipatterns(self.store, self.embedder, self.cfg, self.project, "s1", "/t")
        n2 = service.capture_antipatterns(self.store, self.embedder, self.cfg, self.project, "s2", "/t")
        self.assertEqual(n2, 0)  # same fact id → reinforced, not duplicated
        self.assertEqual(len(self.store.active_antipatterns(self.project["key"])), 1)

    def test_existing_fed_into_extraction(self):
        stub = self._patch([self._rec()])
        service.capture_antipatterns(self.store, self.embedder, self.cfg, self.project, "s1", "/t")
        service.capture_antipatterns(self.store, self.embedder, self.cfg, self.project, "s2", "/t")
        # Second call must have been told about the catalogued anti-pattern (Q6 dedup/refine).
        _text, existing = stub.calls[1]
        self.assertTrue(any("Never put tool flags" in t for _id, t in existing))

    def test_no_records_stores_nothing(self):
        self._patch([])
        n = service.capture_antipatterns(self.store, self.embedder, self.cfg, self.project, "s1", "/t")
        self.assertEqual(n, 0)


class MaybeCaptureGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "proj1", "path": "/tmp/proj1", "label": "proj1"}
        self.transcript = Path(self.tmp.name) / "t.jsonl"
        self.transcript.write_bytes(b"x" * 20000)  # big enough to clear the growth throttle
        self._saved_get_distiller = service.get_distiller
        self._saved_extract_text = service.extract_text

    def tearDown(self):
        service.get_distiller = self._saved_get_distiller
        service.extract_text = self._saved_extract_text
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _patch(self, text):
        stub = _StubDistiller([DistilledFact(text="Never do the bad thing", type="antipattern", scope="project")])
        service.get_distiller = lambda cfg: stub
        service.extract_text = lambda path: text
        return stub

    def test_gate_skips_without_markers(self):
        stub = self._patch("Added a feature and tests; all pass.")
        n = service.maybe_capture_antipatterns(
            self.store, self.embedder, self.cfg, self.project, "s1", str(self.transcript), force=True
        )
        self.assertEqual(n, 0)
        self.assertEqual(stub.calls, [])  # LLM pass never invoked
        # Cursor advanced so a mistake-free stretch isn't re-scanned each turn.
        self.assertEqual(self.store.get_capture_cursor(f"antipat:{self.project['key']}:s1"), 20000)

    def test_gate_runs_with_markers(self):
        stub = self._patch("I was wrong about that; let me correct it.")
        n = service.maybe_capture_antipatterns(
            self.store, self.embedder, self.cfg, self.project, "s1", str(self.transcript), force=True
        )
        self.assertEqual(n, 1)
        self.assertEqual(len(stub.calls), 1)

    def test_throttle_skips_when_not_grown(self):
        small = Path(self.tmp.name) / "small.jsonl"
        small.write_bytes(b"x" * 100)
        stub = self._patch("I mistakenly did it")  # has markers, but growth throttle should win
        n = service.maybe_capture_antipatterns(
            self.store, self.embedder, self.cfg, self.project, "s2", str(small), force=False
        )
        self.assertEqual(n, 0)
        self.assertEqual(stub.calls, [])  # never even read the transcript


class RecallUnionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "projA", "path": "/tmp/projA", "label": "projA"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _add_global_antipattern(self):
        rec = DistilledFact(
            text="Never inject an AI-tool flag like --sandbox into a curl shell command",
            type="antipattern",
            scope="global",
        )
        service.add_records(
            self.store, self.embedder, self.cfg, global_project(), "s1", [rec], kind="antipattern", tier="ltm"
        )

    def test_global_antipattern_in_recall_rows(self):
        self._add_global_antipattern()
        ids = {r["id"] for r in _recall_rows(self.store, self.project["key"])}
        globals_ = {r["id"] for r in self.store.active_antipatterns(GLOBAL_PROJECT_KEY)}
        self.assertTrue(globals_ and globals_ <= ids, "global anti-pattern should be folded into project recall rows")

    def test_no_double_union_for_global_key(self):
        self._add_global_antipattern()
        rows = _recall_rows(self.store, GLOBAL_PROJECT_KEY)
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "global key must not union its own anti-patterns twice")

    def test_global_antipattern_surfaces_in_search(self):
        self._add_global_antipattern()
        hits = search_fused(self.store, self.embedder, self.project, "curl --sandbox flag", self.cfg)
        self.assertTrue(any("--sandbox" in row["text"] for _s, _sim, row in hits))


class LifecycleTests(unittest.TestCase):
    """Phase 2 — anti-patterns are dormancy-exempt but still invalidatable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic", supersede_threshold=0.85)
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "projL", "path": self.tmp.name, "label": "projL"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _add_antipattern(self, text, **kw):
        rec = DistilledFact(text=text, type="antipattern", scope="project", **kw)
        service.add_records(
            self.store, self.embedder, self.cfg, self.project, "s1", [rec], kind="antipattern", tier="ltm"
        )
        return self.store.fact_id(self.project["key"], text)

    def test_refine_exempts_antipatterns(self):
        service.add_facts(
            self.store, self.embedder, self.cfg, self.project, "s1", [f"ordinary fact number {i}" for i in range(4)]
        )
        ap_id = self._add_antipattern("Never force-push over a teammate's branch")
        cfg = replace(self.cfg, refine_keep_max=1)  # keep only the strongest ordinary fact
        pruned = refine(self.store, cfg, self.project)
        self.assertGreater(pruned, 0)  # ordinary facts were pruned
        self.assertEqual(self.store.get(ap_id)["status"], "active")  # the rule survived

    def test_sweep_exempts_antipatterns(self):
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", ["an ordinary stale fact"])
        ap_id = self._add_antipattern("Never delete a migration that has run in production")
        # now far in the future so every row is past the TTL cutoff; keep_frequency high so
        # frequency can't rescue anything — only the kind guard should.
        expired = self.store.sweep(time.time() + 10_000, 0, 999, self.project["key"])
        self.assertGreater(expired, 0)  # the ordinary fact expired
        self.assertEqual(self.store.get(ap_id)["status"], "active")

    def test_displace_exempts_antipatterns(self):
        ap_id = self._add_antipattern("Never commit secrets to the repo")
        service.add_facts(self.store, self.embedder, self.cfg, self.project, "s1", ["stm one", "stm two", "stm three"])
        self.store.displace_stm(self.project["key"], 1)  # cap STM at 1
        self.assertEqual(self.store.get(ap_id)["status"], "active")  # LTM-tier rule untouched

    def test_supersession_retires_old_antipattern(self):
        old_id = self._add_antipattern("Avoid pushing to main")
        # A refined rule that explicitly supersedes the old one.
        rec = DistilledFact(
            text="Never push to main; open a PR", type="antipattern", scope="project", supersedes=[old_id]
        )
        service.add_records(
            self.store, self.embedder, self.cfg, self.project, "s2", [rec], kind="antipattern", tier="ltm"
        )
        self.assertEqual(self.store.get(old_id)["status"], "superseded")
        new_id = self.store.fact_id(self.project["key"], "Never push to main; open a PR")
        self.assertEqual(self.store.get(new_id)["status"], "active")

    def test_drift_invalidates_when_all_files_gone(self):
        ap_id = self._add_antipattern("Never call the legacy helper", files=["ghost/vanished.py"])
        n = invalidate_stale_antipatterns(self.store, self.project)
        self.assertEqual(n, 1)
        self.assertEqual(self.store.get(ap_id)["status"], "expired")

    def test_drift_keeps_rule_when_a_file_exists(self):
        real = Path(self.tmp.name) / "real.py"
        real.write_text("x = 1\n")
        ap_id = self._add_antipattern("Never edit real.py without regenerating", files=["real.py"])
        n = invalidate_stale_antipatterns(self.store, self.project)
        self.assertEqual(n, 0)
        self.assertEqual(self.store.get(ap_id)["status"], "active")

    def test_drift_ignores_pathless_and_fileless(self):
        # A rule with no file anchor is never drift-invalidated.
        ap_id = self._add_antipattern("Never trust unvalidated input")
        self.assertEqual(invalidate_stale_antipatterns(self.store, self.project), 0)
        self.assertEqual(self.store.get(ap_id)["status"], "active")
        # A pathless (global) project yields no filesystem signal.
        self.assertEqual(invalidate_stale_antipatterns(self.store, global_project()), 0)

    def test_consolidate_reports_invalidated(self):
        self._add_antipattern("Never skip the lint step", files=["ghost/gone.py"])
        counts = consolidate(self.store, self.cfg, self.project)
        self.assertIn("invalidated", counts)
        self.assertEqual(counts["invalidated"], 1)


class Phase3PreToolUseTests(unittest.TestCase):
    """Phase 3 — the PreToolUse guard warns before an action that repeats a catalogued mistake."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic", antipatterns=True)
        store = Store(self.cfg.db_path)
        embedder = HashEmbedding(dim=self.cfg.dim)
        rec = DistilledFact(
            text="Never put an AI-tool flag like --sandbox into a curl shell command; set it in the tool schema",
            type="antipattern",
            scope="global",
        )
        service.add_records(store, embedder, self.cfg, global_project(), "s1", [rec], kind="antipattern", tier="ltm")
        store.close()

    def tearDown(self):
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def _sess(self):
        return f"ap3-{os.getpid()}-{self._testMethodName}"

    def test_warns_on_matching_bash(self):
        w = prefer_memory._antipattern_warning(
            self._sess(), "Bash", {"command": "curl -X PUT https://api/data --sandbox disabled"}
        )
        self.assertIsNotNone(w)
        self.assertIn("--sandbox", w)

    def test_dedup_same_rule_once_per_session(self):
        s = self._sess()
        first = prefer_memory._antipattern_warning(s, "Bash", {"command": "curl --sandbox disabled"})
        second = prefer_memory._antipattern_warning(s, "Bash", {"command": "curl --sandbox disabled"})
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # same rule already injected this session

    def test_no_match_returns_none(self):
        self.assertIsNone(prefer_memory._antipattern_warning(self._sess(), "Bash", {"command": "ls -la /tmp"}))

    def test_empty_input_fail_open(self):
        self.assertIsNone(prefer_memory._antipattern_warning(self._sess(), "Bash", {}))

    def test_disabled_by_config(self):
        saved = os.environ.get("ENGRAM_ANTIPATTERNS")
        os.environ["ENGRAM_ANTIPATTERNS"] = "false"
        try:
            self.assertIsNone(
                prefer_memory._antipattern_warning(self._sess(), "Bash", {"command": "curl --sandbox disabled"})
            )
        finally:
            if saved is None:
                os.environ.pop("ENGRAM_ANTIPATTERNS", None)
            else:
                os.environ["ENGRAM_ANTIPATTERNS"] = saved

    def test_hook_end_to_end_emits_context(self):
        # Drive bin/prefer_memory.py as a subprocess: a matching Bash command yields
        # additionalContext and exit 0 (fail-open contract holds).
        env = {
            **os.environ,
            "ENGRAM_DATA_DIR": self.tmp.name,
            "ENGRAM_ANTIPATTERNS": "true",
            "ENGRAM_ENFORCE": "advisory",
        }
        env.pop("ENGRAM_PYTHON", None)
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "curl -X PUT https://api/data --sandbox disabled"},
                "session_id": self._sess(),
            }
        )
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "prefer_memory.py")],
            input=payload,
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertIn("additionalContext", proc.stdout)
        self.assertIn("--sandbox", proc.stdout)


if __name__ == "__main__":
    unittest.main()
