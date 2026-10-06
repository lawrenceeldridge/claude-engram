"""Tests for the claude-mem import path (issue slug: import-claude-mem).

Stdlib unittest, no network. Phase 1 covers the MemorySource port + SourceRecord DTO:
the DTO is a frozen value object with a defaulted timestamp, and the port is an ABC that
cannot be instantiated until all three read methods are implemented (with a default
no-op ``close``). Later phases extend this file with the claude-mem adapter, the bulk
importer, and the CLI.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _harness import ROOT, scoped_env, temp_data_dir

from core import health
from core.adapters.claude_mem_source import (
    ClaudeMemSource,
    _epoch_seconds,
    _json_str_list,
    _merge_files,
    resolve_db_path,
)
from core.config import get_config
from core.migrate import import_memory_source
from core.ports.distill import DistilledFact
from core.ports.embedding import get_embedder
from core.ports.memory_source import MemorySource, SourceRecord, get_memory_source
from core.service import bulk_add_records
from core.store import Store


def _make_claude_mem_db(path: Path, *, observations=(), summaries=()) -> None:
    """Build a tiny claude-mem-shaped SQLite DB with the columns the adapter reads.

    Rebuilds from scratch each call (unlinks any existing file) so a test can re-stage
    the source between successive imports.
    """
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE observations (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, "
        "subtitle TEXT, facts TEXT, narrative TEXT, files_read TEXT, files_modified TEXT, created_at_epoch INTEGER)"
    )
    conn.execute(
        "CREATE TABLE session_summaries (id INTEGER PRIMARY KEY, project TEXT, request TEXT, investigated TEXT, "
        "learned TEXT, completed TEXT, next_steps TEXT, notes TEXT, created_at_epoch INTEGER)"
    )
    for o in observations:
        conn.execute(
            "INSERT INTO observations (id, project, type, title, subtitle, facts, narrative, files_read, "
            "files_modified, created_at_epoch) VALUES (:id,:project,:type,:title,:subtitle,:facts,:narrative,"
            ":files_read,:files_modified,:created_at_epoch)",
            {
                "id": o["id"],
                "project": o.get("project", "ukh-world"),
                "type": o.get("type", "discovery"),
                "title": o.get("title", ""),
                "subtitle": o.get("subtitle", ""),
                "facts": o.get("facts"),
                "narrative": o.get("narrative", ""),
                "files_read": o.get("files_read"),
                "files_modified": o.get("files_modified"),
                "created_at_epoch": o.get("created_at_epoch"),
            },
        )
    for s in summaries:
        conn.execute(
            "INSERT INTO session_summaries (id, project, request, investigated, learned, completed, next_steps, "
            "notes, created_at_epoch) VALUES (:id,:project,:request,:investigated,:learned,:completed,:next_steps,"
            ":notes,:created_at_epoch)",
            {
                "id": s["id"],
                "project": s.get("project", "ukh-world"),
                "request": s.get("request"),
                "investigated": s.get("investigated"),
                "learned": s.get("learned"),
                "completed": s.get("completed"),
                "next_steps": s.get("next_steps"),
                "notes": s.get("notes"),
                "created_at_epoch": s.get("created_at_epoch"),
            },
        )
    conn.commit()
    conn.close()


class SourceRecordTests(unittest.TestCase):
    """The import DTO: frozen, wraps a DistilledFact, defaults its timestamp."""

    def test_wraps_distilled_fact_with_defaults(self):
        rec = SourceRecord(project_label="ukh-world", fact=DistilledFact("a fact"))
        self.assertEqual(rec.project_label, "ukh-world")
        self.assertEqual(rec.fact.text, "a fact")
        self.assertIsNone(rec.created_at_epoch)

    def test_timestamp_is_carried_when_given(self):
        rec = SourceRecord("p", DistilledFact("t"), created_at_epoch=1700000000.0)
        self.assertEqual(rec.created_at_epoch, 1700000000.0)

    def test_is_frozen_value_object(self):
        rec = SourceRecord("p", DistilledFact("t"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            rec.project_label = "other"  # type: ignore[misc]


class MemorySourcePortTests(unittest.TestCase):
    """The port is an ABC — abstract methods are enforced; close() defaults to a no-op."""

    def test_cannot_instantiate_abstract_port(self):
        with self.assertRaises(TypeError):
            MemorySource()  # type: ignore[abstract]

    def test_partial_implementation_still_abstract(self):
        class Partial(MemorySource):
            def available(self) -> bool:
                return True

            # project_labels / iter_records left unimplemented

        with self.assertRaises(TypeError):
            Partial()  # type: ignore[abstract]

    def test_concrete_implementation_and_default_close(self):
        class Fake(MemorySource):
            def available(self) -> bool:
                return True

            def project_labels(self) -> list[str]:
                return ["ukh-world"]

            def iter_records(self, only_label: str | None = None) -> Iterator[SourceRecord]:
                yield SourceRecord("ukh-world", DistilledFact("x"))

        src = Fake()
        self.assertTrue(src.available())
        self.assertEqual(src.project_labels(), ["ukh-world"])
        self.assertEqual([r.fact.text for r in src.iter_records()], ["x"])
        self.assertIsNone(src.close())  # default no-op returns None


class MappingHelperTests(unittest.TestCase):
    """Pure row→DistilledFact helpers (Functional Core), independent of any DB."""

    def test_json_str_list_is_tolerant(self):
        self.assertEqual(_json_str_list('["a","b"]'), ["a", "b"])
        self.assertEqual(_json_str_list("[]"), [])
        self.assertEqual(_json_str_list(None), [])
        self.assertEqual(_json_str_list(""), [])
        self.assertEqual(_json_str_list("not json"), [])
        self.assertEqual(_json_str_list('{"k":1}'), [])  # non-list
        self.assertEqual(_json_str_list('["ok", 3, null]'), ["ok"])  # drop non-strings

    def test_merge_files_unions_and_dedupes_preserving_order(self):
        row = {"files_read": '["a.py","b.py"]', "files_modified": '["b.py","c.py"]'}
        self.assertEqual(_merge_files(row), ["a.py", "b.py", "c.py"])

    def test_epoch_ms_normalised_to_seconds(self):
        self.assertAlmostEqual(_epoch_seconds(1782835033729), 1782835033.729)  # ms → s
        self.assertEqual(_epoch_seconds(1782835033.0), 1782835033.0)  # already seconds
        self.assertIsNone(_epoch_seconds(None))
        self.assertIsNone(_epoch_seconds("nope"))


class AdapterMappingTests(unittest.TestCase):
    """End-to-end mapping through a tiny fixture DB."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "claude-mem.db"

    def _records(self, only_label=None, **kw):
        _make_claude_mem_db(self.db, **kw)
        src = ClaudeMemSource(db_path=self.db)
        try:
            return list(src.iter_records(only_label))
        finally:
            src.close()

    def test_facts_array_fans_out_one_record_each(self):
        recs = self._records(
            observations=[
                {
                    "id": 1,
                    "type": "decision",
                    "title": "T",
                    "subtitle": "S",
                    "narrative": "N",
                    "facts": json.dumps(["fact one", "fact two", "  ", "fact three"]),
                    "files_read": "[]",
                    "files_modified": '["x.py"]',
                    "created_at_epoch": 1782835033729,
                }
            ]
        )
        self.assertEqual([r.fact.text for r in recs], ["fact one", "fact two", "fact three"])  # blank skipped
        f = recs[0].fact
        self.assertEqual((f.title, f.subtitle, f.narrative, f.type), ("T", "S", "N", "decision"))
        self.assertEqual(f.files, ["x.py"])
        self.assertEqual(f.observation_id, "cm-obs-1")
        self.assertAlmostEqual(recs[0].created_at_epoch, 1782835033.729)
        self.assertEqual(recs[0].project_label, "ukh-world")

    def test_empty_facts_falls_back_to_title_subtitle(self):
        recs = self._records(observations=[{"id": 7, "title": "Only title", "subtitle": "sub", "facts": "[]"}])
        self.assertEqual([r.fact.text for r in recs], ["Only title — sub"])
        self.assertEqual(recs[0].fact.observation_id, "cm-obs-7")

    def test_empty_facts_and_no_title_falls_back_to_narrative(self):
        recs = self._records(
            observations=[{"id": 8, "title": "", "subtitle": "", "narrative": "just narrative", "facts": None}]
        )
        self.assertEqual([r.fact.text for r in recs], ["just narrative"])

    def test_wholly_empty_observation_yields_nothing(self):
        recs = self._records(observations=[{"id": 9, "title": "", "subtitle": "", "narrative": "", "facts": "[]"}])
        self.assertEqual(recs, [])

    def test_summary_fields_fan_out_and_skip_empty(self):
        recs = self._records(
            summaries=[
                {
                    "id": 5,
                    "request": "did a thing",
                    "investigated": "",
                    "learned": "learned a lot",
                    "completed": None,
                    "next_steps": "next",
                    "notes": "  ",
                    "created_at_epoch": 1782835033729,
                }
            ]
        )
        self.assertEqual(
            [(r.fact.title, r.fact.text) for r in recs],
            [("request", "did a thing"), ("learned", "learned a lot"), ("next_steps", "next")],
        )
        self.assertTrue(all(r.fact.type == "summary" for r in recs))
        self.assertEqual(
            [r.fact.observation_id for r in recs], ["cm-sum-5-request", "cm-sum-5-learned", "cm-sum-5-next_steps"]
        )

    def test_only_label_filters_both_tables(self):
        recs = self._records(
            only_label="ukh-world",
            observations=[
                {"id": 1, "project": "ukh-world", "facts": json.dumps(["keep"])},
                {"id": 2, "project": "moj-sak", "facts": json.dumps(["drop"])},
            ],
            summaries=[
                {"id": 1, "project": "ukh-world", "learned": "keep-sum"},
                {"id": 2, "project": "moj-sak", "learned": "drop-sum"},
            ],
        )
        self.assertEqual(sorted(r.fact.text for r in recs), ["keep", "keep-sum"])


class AdapterAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def test_absent_db_is_unavailable(self):
        src = ClaudeMemSource(db_path=Path(self.tmp.name) / "nope.db")
        self.assertFalse(src.available())

    def test_empty_file_is_unavailable(self):
        p = Path(self.tmp.name) / "empty.db"
        p.write_bytes(b"")
        self.assertFalse(ClaudeMemSource(db_path=p).available())

    def test_real_fixture_is_available_and_lists_labels(self):
        p = Path(self.tmp.name) / "claude-mem.db"
        _make_claude_mem_db(
            p,
            observations=[{"id": 1, "project": "ukh-world", "facts": json.dumps(["x"])}],
            summaries=[{"id": 1, "project": "moj-sak", "learned": "y"}],
        )
        src = ClaudeMemSource(db_path=p)
        try:
            self.assertTrue(src.available())
            self.assertEqual(sorted(src.project_labels()), ["moj-sak", "ukh-world"])
        finally:
            src.close()


class PathResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        scoped_env(self)  # the tests below set CLAUDE_MEM_DATA_DIR; restored at cleanup

    def test_explicit_path_wins(self):
        self.assertEqual(resolve_db_path("/tmp/x/claude-mem.db"), Path("/tmp/x/claude-mem.db"))

    def test_env_dir_used_when_no_explicit(self):
        os.environ["CLAUDE_MEM_DATA_DIR"] = self.tmp.name
        self.assertEqual(resolve_db_path(), Path(self.tmp.name) / "claude-mem.db")


class FactoryTests(unittest.TestCase):
    def test_returns_claude_mem_source(self):
        src = get_memory_source("claude-mem", db_path="/tmp/x.db")
        self.assertIsInstance(src, ClaudeMemSource)

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            get_memory_source("mystery")


class BulkAddRecordsTests(unittest.TestCase):
    """The bulk writer: batched, idempotent, timestamp-preserving, supersede-free."""

    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = get_embedder(self.cfg)
        self.project = {"key": "testkey00000000a", "label": "t", "path": "/t"}

    def tearDown(self):
        self.store.close()

    def _pairs(self, *texts, ts=None):
        return [(DistilledFact(t), ts) for t in texts]

    def _bulk(self, records, **kw):
        return bulk_add_records(self.store, self.embedder, self.cfg, self.project, "sess", records, **kw)

    def _active(self):
        return len(self.store.active_rows_for_project(self.project["key"]))

    def test_inserts_new_records(self):
        counts = self._bulk(self._pairs("alpha fact", "beta fact", "gamma fact"))
        self.assertEqual(counts["inserted"], 3)
        self.assertEqual(counts["reinforced"], 0)
        self.assertEqual(self._active(), 3)

    def test_idempotent_second_run_reinforces(self):
        self._bulk(self._pairs("alpha", "beta"))
        counts = self._bulk(self._pairs("alpha", "beta"))
        self.assertEqual(counts["inserted"], 0)
        self.assertEqual(counts["reinforced"], 2)
        self.assertEqual(self._active(), 2)

    def test_dedupes_within_a_single_run(self):
        counts = self._bulk(self._pairs("same", "same", "other"))
        self.assertEqual(counts["inserted"], 2)
        self.assertEqual(counts["reinforced"], 1)
        self.assertEqual(self._active(), 2)

    def test_preserves_original_timestamp(self):
        self._bulk([(DistilledFact("timed fact"), 1700000000.0)])
        fid = self.store.fact_id(self.project["key"], "timed fact")
        self.assertEqual(self.store.get(fid)["created_at"], 1700000000.0)

    def test_batching_boundary_inserts_all(self):
        counts = self._bulk(self._pairs("a", "b", "c", "d", "e"), batch=2)
        self.assertEqual(counts["inserted"], 5)
        self.assertEqual(counts["batches"], 3)  # 2 + 2 + 1

    def test_does_not_supersede_near_duplicates(self):
        # add_records archives a near-duplicate; bulk must NOT — superseding is deferred.
        self._bulk(self._pairs("the quick brown fox jumps"))
        self._bulk(self._pairs("the quick brown fox jumps over the lazy dog"))
        self.assertEqual(self._active(), 2)

    def test_progress_called_per_batch(self):
        seen: list[int] = []
        self._bulk(self._pairs("a", "b", "c"), batch=1, progress=lambda c: seen.append(c["inserted"]))
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[-1], 3)


class ImportMemorySourceTests(unittest.TestCase):
    """The orchestrator: label→project resolution, dry-run, skip, only_label, unavailable."""

    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = get_embedder(self.cfg)
        self.db = Path(self.tmp.name) / "cm.db"

    def tearDown(self):
        self.store.close()

    def _source(self, **kw):
        _make_claude_mem_db(self.db, **kw)
        return ClaudeMemSource(db_path=self.db)

    def _proj(self, label):
        return {"key": "k-" + label, "label": label, "path": "/" + label}

    def _import(self, src, resolve=None, **kw):
        return import_memory_source(self.store, self.embedder, self.cfg, src, resolve or self._proj, **kw)

    def test_dry_run_counts_without_writing(self):
        src = self._source(observations=[{"id": 1, "project": "ukh-world", "facts": json.dumps(["x", "y"])}])
        res = self._import(src, dry_run=True)
        self.assertTrue(res["available"])
        self.assertEqual(res["projects"]["ukh-world"]["would_import"], 2)
        self.assertEqual(self.store.count(), 0)
        src.close()

    def test_real_import_writes_facts_under_resolved_key(self):
        src = self._source(
            observations=[{"id": 1, "project": "ukh-world", "facts": json.dumps(["x", "y"])}],
            summaries=[{"id": 1, "project": "ukh-world", "learned": "z"}],
        )
        res = self._import(src)
        self.assertEqual(res["projects"]["ukh-world"]["inserted"], 3)
        self.assertEqual(res["projects"]["ukh-world"]["key"], "k-ukh-world")
        self.assertEqual(self.store.count(), 3)
        src.close()

    def test_low_value_records_are_gated_out(self):
        # Door D: the import bypasses capture, so the ingestion policy runs here — harness blobs,
        # ephemeral CI status and trivial echoes are dropped; a real fact (even short) is kept.
        src = self._source(
            observations=[
                {
                    "id": 1,
                    "project": "ukh-world",
                    "facts": json.dumps(
                        [
                            "TypeScript compilation passed",
                            "<task-notification>agent finished</task-notification>",
                            "Option C",
                            "The auth module uses JWT rotation with a 15m TTL.",
                        ]
                    ),
                }
            ]
        )
        res = self._import(src)
        self.assertEqual(res["projects"]["ukh-world"]["inserted"], 1)
        self.assertEqual(res["projects"]["ukh-world"]["skipped_low_value"], 3)
        self.assertEqual(self.store.count(), 1)
        src.close()

    def test_dry_run_reports_the_gate(self):
        src = self._source(
            observations=[
                {
                    "id": 1,
                    "project": "ukh-world",
                    "facts": json.dumps(["working tree clean", "The deploy target is fly.io."]),
                }
            ]
        )
        res = self._import(src, dry_run=True)
        self.assertEqual(res["projects"]["ukh-world"]["would_import"], 1)
        self.assertEqual(res["projects"]["ukh-world"]["skipped_low_value"], 1)
        self.assertEqual(self.store.count(), 0)
        src.close()

    def test_unknown_label_is_skipped_not_written(self):
        src = self._source(observations=[{"id": 1, "project": "mystery", "facts": json.dumps(["x"])}])
        res = self._import(src, resolve=lambda label: None)
        self.assertIn("mystery", res["skipped"])
        self.assertEqual(self.store.count(), 0)
        src.close()

    def test_only_label_restricts_both_tables(self):
        src = self._source(
            observations=[
                {"id": 1, "project": "ukh-world", "facts": json.dumps(["keep"])},
                {"id": 2, "project": "moj-sak", "facts": json.dumps(["drop"])},
            ]
        )
        res = self._import(src, only_label="ukh-world")
        self.assertEqual(set(res["projects"]), {"ukh-world"})
        self.assertEqual(res["projects"]["ukh-world"]["inserted"], 1)
        self.assertEqual(self.store.count(), 1)
        src.close()

    def test_unavailable_source_reports_cleanly(self):
        src = ClaudeMemSource(db_path=Path(self.tmp.name) / "missing.db")
        res = self._import(src)
        self.assertFalse(res["available"])
        self.assertEqual(self.store.count(), 0)


class ImportCLITests(unittest.TestCase):
    """Drive `engram import claude-mem` as a real subprocess (argparse + cmd_import end-to-end)."""

    def setUp(self):
        self.data = tempfile.TemporaryDirectory()  # ENGRAM_DATA_DIR (engram store)
        self.src = tempfile.TemporaryDirectory()  # source claude-mem DB + mapped project dirs
        self.db = Path(self.src.name) / "claude-mem.db"

    def tearDown(self):
        self.data.cleanup()
        self.src.cleanup()

    def _make(self, **kw):
        _make_claude_mem_db(self.db, **kw)

    def _run(self, *args, input_text=""):
        env = {
            **os.environ,
            "ENGRAM_DATA_DIR": self.data.name,
        }
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "engram"), *args],
            text=True,
            capture_output=True,
            env=env,
            input=input_text,
        )

    def _map_dir(self, name="ukhw"):
        d = Path(self.src.name) / name
        d.mkdir()
        return d

    def _store_counts(self) -> dict[str, int]:
        """{project_key: fact count} from whichever db in the data dir has a facts table."""
        for p in Path(self.data.name).glob("*.db"):
            try:
                conn = sqlite3.connect(p)
                rows = conn.execute("SELECT project_key, COUNT(*) FROM facts GROUP BY project_key").fetchall()
                conn.close()
                return {k: n for k, n in rows}
            except sqlite3.OperationalError:
                continue
        return {}

    def _import_in_process(self, **patches) -> str:
        """``engram import`` run in this process (so ``core.health`` can be patched); returns stderr."""
        loader = importlib.machinery.SourceFileLoader("engram_cli", str(ROOT / "bin" / "engram"))
        cli = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(cli)
        scoped_env(self, ENGRAM_DATA_DIR=self.data.name, ENGRAM_SCORER="python")  # a numpy-less recall scan
        self._make(observations=[{"id": 1, "project": "ukh-world", "facts": json.dumps(["fact a", "fact b"])}])
        argv = [
            "engram",
            "import",
            "claude-mem",
            "--db",
            str(self.db),
            "--map",
            f"ukh-world={self._map_dir()}",
            "--yes",
        ]
        err = io.StringIO()
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.multiple(health, **patches),
            redirect_stdout(io.StringIO()),
            redirect_stderr(err),
        ):
            self.assertEqual(cli.main(), 0)
        return err.getvalue()

    def test_import_warns_when_the_project_is_too_large_to_scan_without_numpy(self):
        self.assertIn("warning: recall scan — no numpy", self._import_in_process(SCAN_WARN_SECONDS=0.0))

    def test_import_is_quiet_when_the_scan_keeps_up(self):
        self.assertNotIn("warning: recall scan", self._import_in_process(SCAN_WARN_SECONDS=health.SCAN_WARN_SECONDS))

    def test_dry_run_reports_and_writes_nothing(self):
        self._make(observations=[{"id": 1, "project": "ukh-world", "facts": json.dumps(["x", "y"])}])
        d = self._map_dir()
        r = self._run("import", "claude-mem", "--db", str(self.db), "--map", f"ukh-world={d}", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[dry-run]", r.stdout)
        self.assertIn("would import", r.stdout)
        self.assertEqual(self._store_counts(), {})  # nothing written

    def test_real_import_with_map_lands_under_resolved_key(self):
        self._make(
            observations=[{"id": 1, "project": "ukh-world", "facts": json.dumps(["fact a", "fact b"])}],
            summaries=[{"id": 1, "project": "ukh-world", "learned": "lesson"}],
        )
        d = self._map_dir()
        expected_key = hashlib.sha256(str(d.resolve()).encode("utf-8")).hexdigest()[:16]
        r = self._run("import", "claude-mem", "--db", str(self.db), "--map", f"ukh-world={d}", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("imported into engram", r.stdout)
        self.assertIn("consolidate", r.stdout)  # closing hint
        self.assertEqual(self._store_counts(), {expected_key: 3})

    def test_reuses_existing_project_key_by_label(self):
        # Pre-seed an engram project labelled ukh-world at a known key, then import with NO --map.
        # The dir basename IS the project label, so map to one named "ukh-world" (mirrors the real
        # case) — reuse-by-label matches the source label against the engram project's stored label.
        self._make(observations=[{"id": 1, "project": "ukh-world", "facts": json.dumps(["new fact"])}])
        d = self._map_dir("ukh-world")
        key = hashlib.sha256(str(d.resolve()).encode("utf-8")).hexdigest()[:16]
        seed = self._run("import", "claude-mem", "--db", str(self.db), "--map", f"ukh-world={d}", "--yes")
        self.assertEqual(seed.returncode, 0, seed.stderr)
        # Second import, no --map: the label must resolve to the existing project's key.
        self._make(observations=[{"id": 2, "project": "ukh-world", "facts": json.dumps(["another fact"])}])
        r = self._run("import", "claude-mem", "--db", str(self.db), "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._store_counts(), {key: 2})  # both facts under the one key

    def test_unmapped_label_is_skipped(self):
        self._make(observations=[{"id": 1, "project": "mystery", "facts": json.dumps(["x"])}])
        r = self._run("import", "claude-mem", "--db", str(self.db), "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skipped", r.stdout)
        self.assertEqual(self._store_counts(), {})

    def test_absent_db_returns_1(self):
        r = self._run("import", "claude-mem", "--db", str(Path(self.src.name) / "missing.db"), "--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no readable", r.stderr)

    def test_non_interactive_without_yes_aborts(self):
        self._make(observations=[{"id": 1, "project": "ukh-world", "facts": json.dumps(["x"])}])
        d = self._map_dir()
        r = self._run("import", "claude-mem", "--db", str(self.db), "--map", f"ukh-world={d}")  # no --yes, piped stdin
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self._store_counts(), {})  # aborted, nothing written

    def test_only_project_restricts_import(self):
        self._make(
            observations=[
                {"id": 1, "project": "ukh-world", "facts": json.dumps(["keep"])},
                {"id": 2, "project": "moj-sak", "facts": json.dumps(["drop"])},
            ]
        )
        uk = self._map_dir("uk")
        mj = self._map_dir("mj")
        uk_key = hashlib.sha256(str(uk.resolve()).encode("utf-8")).hexdigest()[:16]
        r = self._run(
            "import",
            "claude-mem",
            "--db",
            str(self.db),
            "--map",
            f"ukh-world={uk}",
            "--map",
            f"moj-sak={mj}",
            "--project",
            "ukh-world",
            "--yes",
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._store_counts(), {uk_key: 1})  # moj-sak excluded


if __name__ == "__main__":
    unittest.main()
