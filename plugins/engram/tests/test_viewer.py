"""Viewer service-health tests — the header's queue / embedding / distiller chips.

Stdlib unittest, no network: the all-stdlib config (inproc / hash / heuristic) must
report every subsystem healthy, and unreachable probes must fail open to 'warn'
(the configured backend degrades to its stdlib fallback, never a hard error).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.store import Store  # noqa: E402
from viewer.serve import _disambiguate_labels, _service_health, _tcp_ok  # noqa: E402


class DisambiguateLabelsTests(unittest.TestCase):
    def test_colliding_basenames_get_parent_prefix(self):
        items = [
            {"label": "backend", "path": "/x/sak-replicate/backend"},
            {"label": "backend", "path": "/x/sak-assistant/backend"},
            {"label": "claude-engram", "path": "/y/claude-engram"},
        ]
        got = {it["label"] for it in _disambiguate_labels(items)}
        self.assertEqual(got, {"sak-replicate/backend", "sak-assistant/backend", "claude-engram"})

    def test_unique_labels_untouched(self):
        items = [{"label": "a", "path": "/p/a"}, {"label": "b", "path": "/p/b"}]
        self.assertEqual([it["label"] for it in _disambiguate_labels(items)], ["a", "b"])


class PageScriptTests(unittest.TestCase):
    """The viewer page's inline <script> must be valid JS. Guards against Python string
    escaping (e.g. a bare `\\n` in the triple-quoted PAGE) silently corrupting the JS —
    which blanks the whole UI. Skipped when node isn't available."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_served_script_is_valid_js(self):
        from viewer.serve import PAGE

        m = re.search(r"<script>(.*)</script>", PAGE, re.S)
        self.assertIsNotNone(m, "no <script> block in PAGE")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(m.group(1))
            path = fh.name
        result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class TcpOkTests(unittest.TestCase):
    def test_closed_port_is_unreachable(self):
        # Port 1 is not listening — connection refused, fast.
        self.assertFalse(_tcp_ok("http://127.0.0.1:1", timeout=0.2))

    def test_garbage_url_is_unreachable(self):
        self.assertFalse(_tcp_ok("not-a-url", timeout=0.2))
        self.assertFalse(_tcp_ok("http://", timeout=0.2))  # no host


class ServiceHealthTests(unittest.TestCase):
    def _cfg(self, **kw):
        # Pin the stdlib backends regardless of ambient ENGRAM_* env, then override.
        base = replace(get_config(), embedding="hash", distiller="heuristic")
        return replace(base, **kw)

    def test_stdlib_defaults_all_ok(self):
        h = _service_health(self._cfg())
        self.assertEqual(h["queue"]["backend"], "inproc")
        self.assertEqual(h["embedding"]["backend"], "hash")
        self.assertEqual(h["distiller"]["backend"], "heuristic")
        self.assertEqual({s["state"] for s in h.values()}, {"ok"})

    def test_queue_backend_is_always_inproc(self):
        # The WorkQueue has one always-available backend; the chip is never a warn.
        h = _service_health(self._cfg())
        self.assertEqual(h["queue"]["backend"], "inproc")
        self.assertEqual(h["queue"]["state"], "ok")

    def test_llm_distiller_unreachable_warns(self):
        h = _service_health(self._cfg(distiller="ollama", distiller_base_url="http://127.0.0.1:1"))
        self.assertTrue(h["distiller"]["backend"].startswith("ollama"))
        self.assertEqual(h["distiller"]["state"], "warn")
        self.assertIn("heuristic", h["distiller"]["detail"])  # names the fallback


class DeleteProjectTests(unittest.TestCase):
    """The viewer's project-delete route wipes every trace of one project (facts,
    index chunks, work queue, telemetry, cursors, index label) while leaving other
    projects untouched — so a removed project vanishes from every tab."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(Path(self.dir) / "t.db")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed(self, pk: str, label: str) -> None:
        db = self.store.db
        db.execute(
            "INSERT INTO facts (id, project_key, project_label, project_path, session_id, "
            "kind, text, created_at, status, tier) VALUES (?, ?, ?, ?, 's', 'discovery', ?, 1.0, 'active', 'stm')",
            (f"{pk}-f", pk, label, f"/tmp/{pk}", f"fact for {label}"),
        )
        db.execute(
            "INSERT INTO chunks (id, project_key, source_path, kind, anchor, title, heading_path, "
            "level, indexed_at) VALUES (?, ?, 'a.py', 'code_symbol', 'a', 'A', 'A', 0, 1.0)",
            (f"{pk}-c", pk),
        )
        db.execute(
            "INSERT INTO chunk_sources (project_key, source_path, file_hash, mtime_ns, indexed_at) "
            "VALUES (?, 'a.py', 'h', 0, 1.0)",
            (pk,),
        )
        db.execute(
            "INSERT INTO index_meta (project_key, label, path, updated_at) VALUES (?, ?, ?, 1.0)",
            (pk, label, f"/tmp/{pk}"),
        )
        db.commit()
        self.store.log_recall(pk, "q", returned=1, top_sim=0.5, confidence=0.5, verdict="ok")
        self.store.set_capture_cursor(f"{pk}:sess", 10)

    def test_delete_wipes_only_the_target_project(self):
        self._seed("pk1", "ProjOne")
        self._seed("pk2", "ProjTwo")

        counts = self.store.delete_project("pk1")
        self.assertEqual(counts["facts"], 1)
        self.assertEqual(counts["chunks"], 1)

        # pk1 is gone from every table; pk2 is fully intact.
        db = self.store.db
        for table in ("facts", "chunks", "chunk_sources", "recall_events", "index_meta"):
            gone = db.execute(f"SELECT COUNT(*) FROM {table} WHERE project_key = 'pk1'").fetchone()[0]
            kept = db.execute(f"SELECT COUNT(*) FROM {table} WHERE project_key = 'pk2'").fetchone()[0]
            self.assertEqual(gone, 0, f"{table} still has pk1 rows")
            self.assertGreater(kept, 0, f"{table} lost pk2 rows")
        cursors = db.execute("SELECT COUNT(*) FROM capture_cursors WHERE cursor_key LIKE 'pk1:%'").fetchone()[0]
        self.assertEqual(cursors, 0)

        self.assertEqual([r["project_key"] for r in self.store.projects()], ["pk2"])
        self.assertEqual([r["project_key"] for r in self.store.chunk_projects()], ["pk2"])

    def test_delete_missing_project_is_a_noop(self):
        self._seed("pk1", "ProjOne")
        self.assertEqual(self.store.delete_project("nope"), {"facts": 0, "chunks": 0, "work_queue": 0})
        self.assertEqual([r["project_key"] for r in self.store.projects()], ["pk1"])


class DeleteMemoryTests(unittest.TestCase):
    """delete_memory removes one viewer card — a whole observation group (all facts
    sharing observation_id) or a single ungrouped fact — leaving other memories intact."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(Path(self.dir) / "t.db")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _fact(self, fid: str, text: str, obs: str | None = None) -> None:
        self.store.db.execute(
            "INSERT INTO facts (id, project_key, project_label, project_path, session_id, kind, "
            "text, observation_id, created_at, status, tier) "
            "VALUES (?, 'pk', 'P', '/p', 's', 'discovery', ?, ?, 1.0, 'active', 'stm')",
            (fid, text, obs),
        )
        self.store.db.commit()

    def test_delete_observation_group_removes_all_its_facts(self):
        self._fact("a1", "alpha one", obs="obs-A")
        self._fact("a2", "alpha two", obs="obs-A")
        self._fact("b1", "beta one", obs="obs-B")
        removed = self.store.delete_memory("obs-A")
        self.assertEqual(removed, 2)
        left = [r["id"] for r in self.store.db.execute("SELECT id FROM facts ORDER BY id")]
        self.assertEqual(left, ["b1"])  # the other observation is untouched

    def test_delete_single_ungrouped_fact_by_id(self):
        self._fact("solo", "a lone fact", obs=None)
        self._fact("keep", "keep me", obs=None)
        self.assertEqual(self.store.delete_memory("solo"), 1)
        self.assertEqual([r["id"] for r in self.store.db.execute("SELECT id FROM facts")], ["keep"])

    def test_delete_stays_searchable_via_fts_after(self):
        # FTS must stay consistent after a delete (external-content trigger).
        self._fact("a1", "alpha searchable", obs="obs-A")
        self._fact("b1", "beta searchable", obs="obs-B")
        self.store.delete_memory("obs-A")
        hits = list(self.store.db.execute("SELECT rowid FROM facts_fts WHERE facts_fts MATCH 'searchable'"))
        self.assertEqual(len(hits), 1)  # only beta remains, no dangling FTS row

    def test_delete_missing_key_is_a_noop(self):
        self._fact("a1", "alpha", obs="obs-A")
        self.assertEqual(self.store.delete_memory("nope"), 0)
        self.assertEqual(self.store.delete_memory(""), 0)


if __name__ == "__main__":
    unittest.main()
