"""Incremental-capture tests — per-session cursor so each Stop distils only the delta.

Run: python3 -m unittest discover -s plugins/ltm/tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import Store  # noqa: E402
from core.transcript import extract_incremental  # noqa: E402


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
        text1, off1 = extract_incremental(self.f.name, 0)
        self.assertIn("Postgres", text1)

        # Nothing new yet.
        text2, off2 = extract_incremental(self.f.name, off1)
        self.assertEqual(text2, "")
        self.assertEqual(off2, off1)

        # Append a turn; only the new turn comes back.
        with open(self.f.name, "a", encoding="utf-8") as fh:
            fh.write(_turn("assistant", "Switched the cache to Redis."))
        text3, off3 = extract_incremental(self.f.name, off1)
        self.assertIn("Redis", text3)
        self.assertNotIn("Postgres", text3)
        self.assertGreater(off3, off1)

    def test_truncation_resets_offset(self):
        self.f.write(_turn("assistant", "some content"))
        self.f.flush()
        _text, off = extract_incremental(self.f.name, 0)
        text, new_off = extract_incremental(self.f.name, off + 10_000)  # offset past EOF
        self.assertIn("some content", text)
        self.assertLessEqual(new_off, off)


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.store = Store(get_config().db_path)

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_cursor_roundtrip_and_default_zero(self):
        self.assertEqual(self.store.get_capture_cursor("sess-x"), 0)
        self.store.set_capture_cursor("sess-x", 4096)
        self.assertEqual(self.store.get_capture_cursor("sess-x"), 4096)
        self.store.set_capture_cursor("sess-x", 8192)
        self.assertEqual(self.store.get_capture_cursor("sess-x"), 8192)


class IncrementalCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}
        self.tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()
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
