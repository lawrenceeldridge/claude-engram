"""No connection is ever left inside a write transaction (#66 part 3).

On 2026-10-05 a long-lived MCP server held the store's write lock for ~13 h: a Store write that
failed after Python's implicit ``BEGIN`` left its connection mid-transaction, and nothing rolled
it back. Every Store write now runs inside ``with self.db`` (commit, or roll back on any error),
and the long-lived processes roll back anything still open after each request — and log it.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _harness import ROOT, temp_data_dir

from core import errlog, service
from core.config import get_config
from core.ports.embedding import HashEmbedding
from core.store import Store

_DML = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
_CALLED_INSIDE_A_CALLERS_TRANSACTION = {"_insert_chunk_rows"}  # private helper; every caller wraps it
_SCHEMA = {"_migrate"}  # the once-per-open migration ladder, committed per step


def _sql_of(call: ast.Call) -> str:
    arg = call.args[0] if call.args else None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        return "".join(v.value for v in arg.values if isinstance(v, ast.Constant))
    return ""


class StoreWritesAreTransactionalTests(unittest.TestCase):
    def test_every_store_write_runs_inside_with_self_db(self):
        tree = ast.parse((ROOT / "core" / "store.py").read_text(encoding="utf-8"))
        store_cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Store")
        for fn in store_cls.body:
            if not isinstance(fn, ast.FunctionDef) or fn.name in _SCHEMA | _CALLED_INSIDE_A_CALLERS_TRANSACTION:
                continue
            guarded = [
                range(w.lineno, w.end_lineno + 1)
                for w in ast.walk(fn)
                if isinstance(w, ast.With) and any(ast.unparse(item.context_expr) == "self.db" for item in w.items)
            ]
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and ast.unparse(call.func) in ("self.db.execute", "self.db.executemany"):
                    if _DML.match(_sql_of(call)):
                        with self.subTest(method=fn.name, line=call.lineno):
                            self.assertTrue(
                                any(call.lineno in span for span in guarded), "wrap the write in `with self.db:`"
                            )
            self.assertNotIn("self.db.commit()", ast.unparse(fn), f"{fn.name}: commit via `with self.db:`, not by hand")


class FailedWriteTests(unittest.TestCase):
    def setUp(self):
        temp_data_dir(self)
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.addCleanup(self.store.close)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}
        service.add_facts(self.store, HashEmbedding(dim=self.cfg.dim), self.cfg, self.project, "s1", ["a durable fact"])

    def _abort(self, when: str, table: str) -> None:
        self.store.db.execute(
            f"CREATE TEMP TRIGGER abort_{when}_{table} BEFORE {when} ON {table} BEGIN SELECT RAISE(ABORT, 'simulated'); END"
        )

    def test_a_failed_best_effort_ledger_write_leaves_no_transaction_open(self):
        self._abort("INSERT", "recall_events")
        self._abort("INSERT", "usage_events")
        self.store.log_recall("p", "q", returned=1, top_sim=0.5, confidence=0.5, verdict="ok")  # swallowed
        self.store.record_usage("p", "inject_prompt", bytes_in=10)  # swallowed
        self.assertFalse(self.store.db.in_transaction)

    def test_a_failed_write_raises_rolls_back_and_releases_the_lock(self):
        fid = self.store.active_rows_for_project("p")[0]["id"]
        self._abort("UPDATE", "facts")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.set_status([fid], "expired")
        self.assertFalse(self.store.db.in_transaction)
        other = sqlite3.connect(self.cfg.db_path, timeout=0.2)  # another process can write at once
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        other.close()

    def test_end_stray_transaction(self):
        self.assertFalse(self.store.end_stray_transaction())
        self.store.db.execute("INSERT INTO recall_events (ts, project_key, query) VALUES (0, 'p', 'left open')")
        self.assertTrue(self.store.db.in_transaction)
        self.assertTrue(self.store.end_stray_transaction())
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM recall_events").fetchone()[0], 0)


class McpServerGuardTests(unittest.TestCase):
    def setUp(self):
        self.data = temp_data_dir(self)
        import mcp_server

        self.mcp = mcp_server
        self.mcp.ENGINE = mcp_server._Engine()
        self.mcp.ENGINE._init()
        engine = self.mcp.ENGINE
        project = engine._project(None)
        service.add_facts(
            engine.store, engine.embedder, engine.cfg, project, "s1", ["The deploy pipeline uses GitHub Actions."]
        )
        self.fact_id = engine.store.active_rows_for_project(project["key"])[0]["id"]
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        Path(repo.name, "mod.py").write_text("def deploy():\n    return 1\n", encoding="utf-8")
        self.repo = repo.name

    def _call(self, name: str, arguments: dict) -> None:
        self.mcp._handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        )

    def test_no_tool_leaves_a_transaction_open(self):
        calls = {
            "recall": {"query": "deploy pipeline"},
            "list_projects": {},
            "index_docs": {"path": self.repo},
            "search_docs": {"query": "deploy"},
            "search_code": {"query": "deploy"},
            "search_history": {"query": "deploy"},
            "get_symbol": {"anchor": "deploy"},
            "get_doc_section": {"anchor": "nothing"},
            "doc_outline": {},
            "code_outline": {},
            "review_memory": {"query": "deploy"},
            "invalidate_memory": {"ids": [self.fact_id]},
        }
        self.assertEqual(set(calls), {tool["name"] for tool in self.mcp.TOOLS})  # a new tool must be added here
        for name, arguments in calls.items():
            with self.subTest(tool=name):
                self._call(name, arguments)
                self.assertFalse(self.mcp.ENGINE.store.db.in_transaction)

    def test_settle_rolls_back_a_stray_transaction_and_records_it(self):
        engine = self.mcp.ENGINE
        engine.store.db.execute("INSERT INTO recall_events (ts, project_key, query) VALUES (0, 'p', 'left open')")
        engine.settle()
        self.assertFalse(engine.store.db.in_transaction)
        self.assertEqual(errlog.last(engine.cfg.data_dir)["source"], "mcp_server")
        engine.settle()  # nothing open: nothing more recorded
        self.assertEqual(errlog.log_path(engine.cfg.data_dir).read_text().count("\n"), 1)


class ErrlogTests(unittest.TestCase):
    def setUp(self):
        self.dir = temp_data_dir(self).name

    def test_records_and_reads_back_the_newest_event(self):
        self.assertIsNone(errlog.last(self.dir))
        errlog.record(self.dir, "capture", "first", now=1.0)
        errlog.record(self.dir, "capture", "second", now=2.0)
        self.assertEqual(errlog.last(self.dir), {"ts": 2.0, "source": "capture", "message": "second"})

    def test_rotates_past_the_cap_so_it_stays_bounded(self):
        with mock.patch.object(errlog, "MAX_BYTES", 200):
            for i in range(50):
                errlog.record(self.dir, "capture", f"event {i:03d} " + "x" * 40)
        current, rotated = errlog.log_path(self.dir), errlog.log_path(self.dir).with_name(errlog.LOG_NAME + ".1")
        self.assertTrue(rotated.exists())
        self.assertLess(current.stat().st_size, 200 + 100)
        self.assertIn("event 049", current.read_text())

    def test_never_raises(self):
        errlog.record("/nonexistent/dir/for/sure", "capture", "lost")  # unwritable: swallowed
        self.assertIsNone(errlog.last("/nonexistent/dir/for/sure"))
        errlog.log_path(self.dir).write_bytes(b"\xff\xfe not json\n")
        self.assertIsNone(errlog.last(self.dir))


if __name__ == "__main__":
    unittest.main()
