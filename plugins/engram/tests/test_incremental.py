"""Incremental-capture tests — per-session cursor so each Stop distils only the delta.

Run: python3 -m unittest discover -s plugins/engram/tests
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace

from _harness import temp_data_dir

from core import service
from core.config import get_config
from core.ports.embedding import HashEmbedding
from core.store import Store
from core.transcript import extract_incremental_parts


def _turn(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}) + "\n"


class ExtractIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")

    def tearDown(self):
        os.unlink(self.f.name)

    def test_reads_only_appended_content(self):
        self.f.write(_turn("assistant", "The project uses Postgres for storage."))
        self.f.flush()
        first = extract_incremental_parts(self.f.name, 0)
        self.assertIn("Postgres", first.text)
        self.assertEqual((first.start, first.turns), (0, [("assistant", "The project uses Postgres for storage.")]))

        # Nothing new yet.
        idle = extract_incremental_parts(self.f.name, first.end)
        self.assertEqual((idle.text, idle.turns, idle.end), ("", [], first.end))

        # Append a turn; only the new turn comes back, and the span starts where the last ended.
        with open(self.f.name, "a", encoding="utf-8") as fh:
            fh.write(_turn("assistant", "Switched the cache to Redis."))
        later = extract_incremental_parts(self.f.name, first.end)
        self.assertIn("Redis", later.text)
        self.assertNotIn("Postgres", later.text)
        self.assertEqual(later.start, first.end)
        self.assertGreater(later.end, first.end)

    def test_truncation_resets_offset(self):
        self.f.write(_turn("assistant", "some content"))
        self.f.flush()
        off = extract_incremental_parts(self.f.name, 0).end
        reread = extract_incremental_parts(self.f.name, off + 10_000)  # offset past EOF
        self.assertIn("some content", reread.text)
        self.assertEqual(reread.start, 0)  # a shrunk file resets to the start
        self.assertLessEqual(reread.end, off)


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.store = Store(get_config().db_path)

    def tearDown(self):
        self.store.close()

    def test_cursor_roundtrip_and_default_zero(self):
        self.assertEqual(self.store.get_capture_cursor("sess-x"), 0)
        self.store.set_capture_cursor("sess-x", 4096)
        self.assertEqual(self.store.get_capture_cursor("sess-x"), 4096)
        self.store.set_capture_cursor("sess-x", 8192)
        self.assertEqual(self.store.get_capture_cursor("sess-x"), 8192)


class IncrementalCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}
        self.tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")

    def tearDown(self):
        self.store.close()
        os.unlink(self.tf.name)

    def _cap(self):
        return service.capture_transcript_incremental(
            self.store, self.embedder, self.cfg, self.project, "sess-1", self.tf.name
        )

    def test_second_capture_with_no_new_turns_is_zero(self):
        self.tf.write(_turn("assistant", "Adopted the repository pattern for data access."))
        self.tf.flush()
        self.assertGreaterEqual(self._cap(), 1)
        self.assertEqual(self._cap(), 0)  # nothing new — cursor already at EOF

    def test_only_new_turn_is_distilled(self):
        self.tf.write(_turn("assistant", "Adopted the repository pattern for data access."))
        self.tf.flush()
        self._cap()
        before = {r["text"] for r in self.store.active_rows_for_project(self.project["key"])}
        with open(self.tf.name, "a", encoding="utf-8") as fh:
            fh.write(_turn("assistant", "Added a Redis cache in front of the pipeline results."))
        self._cap()
        after = {r["text"] for r in self.store.active_rows_for_project(self.project["key"])}
        new = after - before
        self.assertTrue(any("Redis" in t for t in new))


if __name__ == "__main__":
    unittest.main(verbosity=2)
