"""Snapshot index kind (Phase 3a) — a page a11y snapshot indexed as a durable ``snapshot``
chunk (the index's visual long-term-store column). Stdlib unittest, hash embedder, no network.

Verifies: the snapshot lands in the chunk pipeline under kind='snapshot'; search is kind-scoped
(isolated from code/docs); re-indexing a URL replaces (one chunk per URL); freshness is age-based
(not file-drift → never 'gone'); the snapshot is exempt from file drift-reconciliation; and it
never touches the ``facts`` recall surface (A-S modality routing — visual → index, not facts).
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _harness  # noqa: F401

from core.config import get_config
from core.index.index_recall import _snapshot_freshness, search_index
from core.index.indexer import index_snapshot
from core.ports.embedding import HashEmbedding
from core.store import Store

_A11Y = 'heading "Login"\ntextbox "Email"\nbutton "Sign in"\nlink "Forgot password"'


class SnapshotIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = replace(get_config(), embedding="hash")
        self.store = Store(Path(self.tmp.name) / "memory.db")
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "proj", "path": self.tmp.name, "label": "proj"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _index(self, url=" https://x/app", text=_A11Y, now=1000.0):
        return index_snapshot(self.store, self.embedder, self.cfg, self.project, url.strip(), text, now=now)

    def test_indexes_as_snapshot_kind_chunk(self):
        res = self._index()
        self.assertEqual(res["status"], "indexed")
        self.assertEqual(res["chunks"], 1)
        rows = self.store.chunk_rows(self.project["key"], kind="snapshot")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_path"], "https://x/app")
        self.assertEqual(rows[0]["body"], _A11Y)

    def test_empty_snapshot_is_noop(self):
        res = self._index(text="   \n ")
        self.assertEqual(res["status"], "empty")
        self.assertEqual(self.store.chunk_rows(self.project["key"], kind="snapshot"), [])

    def test_search_is_kind_scoped_and_isolated(self):
        self._index()
        hits = search_index(self.store, self.embedder, self.cfg, self.project, "login sign in", kind="snapshot")
        self.assertEqual(hits["returned"], 1)
        self.assertEqual(hits["results"][0]["kind"], "snapshot")
        # isolation: the code/doc kinds do not surface the snapshot
        self.assertEqual(
            search_index(self.store, self.embedder, self.cfg, self.project, "login", kind="code_symbol")["returned"], 0
        )
        self.assertEqual(
            search_index(self.store, self.embedder, self.cfg, self.project, "login", kind="doc_section")["returned"], 0
        )
        # unscoped search includes it
        self.assertEqual(
            search_index(self.store, self.embedder, self.cfg, self.project, "login sign in")["returned"], 1
        )

    def test_reindex_same_url_replaces(self):
        self._index(text=_A11Y, now=1000.0)
        self._index(text='heading "Dashboard"\nbutton "Log out"', now=1001.0)  # same URL, new state
        rows = self.store.chunk_rows(self.project["key"], kind="snapshot")
        self.assertEqual(len(rows), 1)  # one chunk per URL — latest perception
        self.assertIn("Dashboard", rows[0]["body"])

    def test_distinct_urls_are_distinct_chunks(self):
        self._index(url="https://x/a", now=1000.0)
        self._index(url="https://x/b", now=1000.0)
        self.assertEqual(len(self.store.chunk_rows(self.project["key"], kind="snapshot")), 2)

    def test_freshness_is_age_based(self):
        # pure helper: fresh when recent, stale when old — never file-drift
        self.assertEqual(_snapshot_freshness(1000.0, 1000.0), "fresh")
        self.assertEqual(_snapshot_freshness(1000.0, 1000.0 + 90_000), "stale")
        # a just-indexed snapshot reads 'fresh' in search (a file-based check would say 'gone')
        self._index(now=None)
        hits = search_index(self.store, self.embedder, self.cfg, self.project, "login sign in", kind="snapshot")
        self.assertEqual(hits["results"][0]["freshness"], "fresh")

    def test_exempt_from_file_drift_reconciliation(self):
        self._index()
        # a snapshot writes NO chunk_sources row, so index_project's drift purge
        # (indexed_sources - seen) can never target it.
        self.assertNotIn("https://x/app", self.store.indexed_sources(self.project["key"]))

    def test_never_enters_facts_recall_surface(self):
        self._index()
        # A-S modality routing: a visual perception goes to the index, not the verbal facts store.
        self.assertEqual(self.store.active_count(self.project["key"]), 0)


if __name__ == "__main__":
    unittest.main()
