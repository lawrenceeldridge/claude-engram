"""Hook output reaches Claude Code, and the interactive hooks never wait on another writer.

Claude Code reads a JSON hook output's ``additionalContext`` only under ``hookSpecificOutput``
(with ``hookEventName``); engram's recall hooks printed it top-level, so none of it reached the
model. And every ``Store()`` open ran ``CREATE … IF NOT EXISTS`` — which takes the write lock —
so on a contended store the prompt hook spent its whole 5 s ceiling queueing (87/87 prompts timed
out in one session). These pin both fixes.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from _harness import ROOT, temp_data_dir

import _bootstrap

from core import errlog, health, service
from core.config import get_config
from core.ports.embedding import HashEmbedding
from core.project import resolve_project
from core.store import Store

FACTS = ["The deploy pipeline runs on GitHub Actions and ships to AWS Lambda.", "Embeddings are quantised to int8."]


def _emitted(event: str, **kwargs) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        _bootstrap.emit(event, **kwargs)
    return out.getvalue()


class EmitTests(unittest.TestCase):
    def test_event_fields_nest_under_hook_specific_output(self):
        out = json.loads(_emitted("UserPromptSubmit", additionalContext="ctx"))
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}})

    def test_system_message_is_top_level_and_alone_is_enough(self):
        out = json.loads(_emitted("SessionStart", additionalContext="ctx", system_message="warn"))
        self.assertEqual(out["systemMessage"], "warn")
        self.assertEqual(json.loads(_emitted("SessionStart", system_message="warn")), {"systemMessage": "warn"})

    def test_nothing_to_say_prints_nothing(self):
        self.assertEqual(_emitted("UserPromptSubmit", additionalContext=""), "")


class _HookFixture(unittest.TestCase):
    """A data dir with two facts, and the hook scripts run as Claude Code runs them."""

    def setUp(self):
        temp_data_dir(self)
        self.cfg = replace(get_config(), distiller="heuristic")
        store = Store(self.cfg.db_path)
        self.project = resolve_project(str(ROOT), self.cfg.markers, identity="marker")
        for text in FACTS:
            service.add_facts(store, HashEmbedding(dim=self.cfg.dim), self.cfg, self.project, "s1", [text])
        store.close()

    def _hook(self, script: str, payload: dict) -> tuple[subprocess.CompletedProcess, float]:
        start = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / script)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=30,
            env={**os.environ, "ENGRAM_IDENTITY": "marker"},
        )
        return proc, time.perf_counter() - start

    def _context(self, proc: subprocess.CompletedProcess, event: str) -> str:
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertNotIn("additionalContext", out)  # the shape Claude Code ignored
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], event)
        return out["hookSpecificOutput"]["additionalContext"]


class HookOutputTests(_HookFixture):
    def test_the_prompt_hook_injects_in_the_documented_shape(self):
        proc, _ = self._hook("recall_prompt.py", {"prompt": "how do we deploy to aws lambda", "cwd": str(ROOT)})
        self.assertIn("GitHub Actions", self._context(proc, "UserPromptSubmit"))

    def test_the_session_start_hook_injects_in_the_documented_shape(self):
        proc, _ = self._hook("recall_session_start.py", {"cwd": str(ROOT)})
        self.assertIn("[engram] Memory/index-first", self._context(proc, "SessionStart"))

    def test_the_prompt_hook_still_injects_while_another_connection_holds_the_write_lock(self):
        holder = sqlite3.connect(self.cfg.db_path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO recall_events (ts, project_key, query) VALUES (0, 'x', 'held')")
        try:
            proc, seconds = self._hook(
                "recall_prompt.py", {"prompt": "how do we deploy to aws lambda", "cwd": str(ROOT)}
            )
        finally:
            holder.rollback()
            holder.close()
        self.assertIn("GitHub Actions", self._context(proc, "UserPromptSubmit"))
        self.assertLess(seconds, 4.0)  # was: ≥ 5 s waiting out busy timeouts, then cancelled


class SessionNoticeTests(_HookFixture):
    """SessionStart tells the user (``systemMessage``) only when something is wrong."""

    def _start(self) -> dict:
        proc, _ = self._hook("recall_session_start.py", {"cwd": str(ROOT)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_a_healthy_start_carries_no_notice(self):
        self.assertNotIn("systemMessage", self._start())

    def test_a_recent_worker_error_is_shown_to_the_user(self):
        errlog.record(self.cfg.data_dir, "capture", "transcript: OperationalError('database is locked')")
        out = self._start()
        self.assertIn("database is locked", out["systemMessage"])
        self.assertIn("engram doctor", out["systemMessage"])
        self.assertIn(
            "[engram] Memory/index-first", out["hookSpecificOutput"]["additionalContext"]
        )  # context unchanged

    def test_a_write_locked_store_is_shown_not_hung_on(self):
        holder = sqlite3.connect(self.cfg.db_path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO recall_events (ts, project_key, query) VALUES (0, 'x', 'held')")
        try:
            proc, seconds = self._hook("recall_session_start.py", {"cwd": str(ROOT)})
        finally:
            holder.rollback()
            holder.close()
        self.assertIn("write-locked", json.loads(proc.stdout)["systemMessage"])
        self.assertLess(seconds, 4.0)

    def test_the_session_checks_are_cheap(self):
        store = Store(self.cfg.db_path)
        try:
            start = time.perf_counter()
            self.assertEqual(health.session_warnings(self.cfg, store), [])
            self.assertLess(time.perf_counter() - start, 0.05)
        finally:
            store.close()


class StoreOpenTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(temp_data_dir(self).name) / "memory.db"
        Store(self.path).close()  # a current store

    def test_opening_a_current_store_does_not_wait_for_the_write_lock(self):
        holder = sqlite3.connect(self.path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO recall_events (ts, project_key, query) VALUES (0, 'x', 'held')")
        try:
            start = time.perf_counter()
            Store(self.path).close()
            self.assertLess(time.perf_counter() - start, 0.5)
        finally:
            holder.rollback()
            holder.close()

    def test_a_store_missing_a_schema_object_is_repaired_on_open(self):
        conn = sqlite3.connect(self.path)
        conn.execute("DROP INDEX idx_recall_project")
        conn.commit()
        conn.close()
        store = Store(self.path)
        try:
            names = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master")}
        finally:
            store.close()
        self.assertIn("idx_recall_project", names)

    def test_interactive_writes_fail_fast_under_a_held_lock(self):
        from core.store import INTERACTIVE_BUSY_MS

        holder = sqlite3.connect(self.path)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO recall_events (ts, project_key, query) VALUES (0, 'x', 'held')")
        store = Store(self.path, busy_timeout_ms=INTERACTIVE_BUSY_MS)
        try:
            start = time.perf_counter()
            store.log_recall("p", "q", returned=1, top_sim=0.5, confidence=0.5, verdict="ok")  # best-effort
            self.assertLess(time.perf_counter() - start, 1.0)
            self.assertFalse(store.db.in_transaction)
        finally:
            store.close()
            holder.rollback()
            holder.close()


if __name__ == "__main__":
    unittest.main()
