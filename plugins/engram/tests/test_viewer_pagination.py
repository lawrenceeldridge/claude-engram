"""Viewer pagination + loading-feedback surface.

Covers the changes from the `viewer-pagination` plan:
- the loading indicator wiring in PAGE (top bar + "loading more…" pill + fail-safe clear),
- the unified `loadNextPage` pager that drives infinite scroll for every browse panel,
- Store-level paging/counters that back the Consolidation and Sensory panels.

Stdlib unittest, no network. The PAGE's JS is separately syntax-checked by
test_viewer.PageScriptTests via `node --check`; here we assert the wiring is present.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

from core import service  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.recall import search_fused  # noqa: E402
from core.store import Store  # noqa: E402
from viewer.serve import PAGE, SEARCH_K_CAP  # noqa: E402


class _SeededStore(unittest.TestCase):
    """A store with a helper to insert facts at a chosen status / group / time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fact(self, fid, text, *, status="active", obs=None, created=1.0, pk="pk"):
        self.store.db.execute(
            "INSERT INTO facts (id, project_key, project_label, project_path, session_id, kind, "
            "text, observation_id, created_at, status, tier) "
            "VALUES (?, ?, 'P', '/p', 's', 'discovery', ?, ?, ?, ?, 'stm')",
            (fid, pk, text, obs, created, status),
        )
        self.store.db.commit()


class ArchivedCountAndPagingTests(_SeededStore):
    """Phase 2 — the Consolidation panel pages archived facts and shows a group-accurate total."""

    def test_archived_count_counts_groups_not_facts(self):
        # One archived group of two facts + one archived lone fact = 2 cards, 3 rows.
        self._fact("a1", "alpha one", status="superseded", obs="obs-A", created=1.0)
        self._fact("a2", "alpha two", status="superseded", obs="obs-A", created=2.0)
        self._fact("b1", "beta", status="pruned", obs=None, created=3.0)
        self._fact("c1", "active fact", status="active", obs=None, created=4.0)  # excluded
        self.assertEqual(self.store.archived_count("pk"), 2)  # groups, not the 3 archived rows
        # Matches what the panel actually renders (one card per group).
        self.assertEqual(len(self.store.list_observations("pk", active=False)), 2)

    def test_archived_count_zero_when_none_archived(self):
        self._fact("c1", "active", status="active", created=1.0)
        self.assertEqual(self.store.archived_count("pk"), 0)

    def test_archived_pages_union_to_the_full_set_without_overlap(self):
        for i in range(5):
            self._fact(f"f{i}", f"archived {i}", status="pruned", obs=None, created=float(i))
        self.assertEqual(self.store.archived_count("pk"), 5)
        page1 = self.store.list_observations("pk", active=False, limit=2, offset=0)
        page2 = self.store.list_observations("pk", active=False, limit=2, offset=2)
        page3 = self.store.list_observations("pk", active=False, limit=2, offset=4)
        self.assertEqual([len(page1), len(page2), len(page3)], [2, 2, 1])  # short last page

        def ids(groups):
            return [rows[0]["id"] for rows in groups]

        seen = ids(page1) + ids(page2) + ids(page3)
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)  # no duplicates across pages


class ConsolidationPagePagingTests(unittest.TestCase):
    """Phase 2 — the served page wires archived pagination through the unified pager."""

    def test_consolidation_fetches_first_page_with_limit_offset(self):
        self.assertIn("/api/consolidation?project=", PAGE)
        self.assertIn("&limit=${PAGE}&offset=0", PAGE)

    def test_consolidation_pager_and_total_wired(self):
        self.assertIn("async function loadConsolidationPage()", PAGE)
        self.assertIn("loadNextPage = loadConsolidationPage;", PAGE)
        self.assertIn("r.archived_total", PAGE)


class SensoryPagePagingTests(unittest.TestCase):
    """Phase 3 — the Sensory panel pages through the same unified pager."""

    def test_sensory_fetches_first_page_with_limit_offset(self):
        self.assertIn("/api/sensory?project=", PAGE)
        self.assertIn("loadNextPage = loadSensoryPage;", PAGE)

    def test_sensory_pager_present(self):
        self.assertIn("async function loadSensoryPage()", PAGE)
        # total drives has-more, so a decayed/short page stops the scroll.
        self.assertIn("exhausted = offset >= total", PAGE)


class SearchKCapTests(unittest.TestCase):
    """Phase 4 — capping the viewer's fused-search k is truncation only: the capped top-k
    is an exact prefix of the uncapped ranking. Because k also sizes the FTS candidate pool
    (fts_search limit = max(k*4, 50)), this is verified with a corpus larger than the capped
    pool, so a reordering bug (not just fewer rows) would fail the assertion."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ENGRAM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "test", "path": "/tmp/test", "label": "test"}

    def tearDown(self):
        self.store.close()
        os.environ.pop("ENGRAM_DATA_DIR", None)
        self.tmp.cleanup()

    def test_capped_search_is_exact_prefix_of_uncapped(self):
        cap = 10
        n = 60  # > max(cap*4, 50) = 50, so the capped FTS pool is genuinely smaller
        # One fact per call: a single add_facts batch would distil to one observation.
        for i in range(n):
            service.add_facts(
                self.store,
                self.embedder,
                self.cfg,
                self.project,
                f"s{i}",
                [f"deploy pipeline variant {i} rollout step number {i}"],
            )
        self.assertEqual(self.store.active_count("test"), n)
        uncapped = search_fused(self.store, self.embedder, self.project, "deploy pipeline", self.cfg, k=n)
        capped = search_fused(self.store, self.embedder, self.project, "deploy pipeline", self.cfg, k=cap)
        ids_uncapped = [row["id"] for _s, _sim, row in uncapped]
        ids_capped = [row["id"] for _s, _sim, row in capped]
        self.assertEqual(len(ids_capped), cap)
        self.assertEqual(ids_capped, ids_uncapped[:cap])  # same top hits, just fewer

    def test_cap_constant_matches_index_search(self):
        # Kept equal to the index search's k=200 so the two search surfaces behave alike.
        self.assertEqual(SEARCH_K_CAP, 200)


class LoadingIndicatorPageTests(unittest.TestCase):
    """Phase 1 — every panel/project/page switch shows feedback and clears in finally."""

    def test_loading_elements_present(self):
        self.assertIn('id="loadbar"', PAGE)
        self.assertIn('id="loadmore"', PAGE)
        self.assertIn("is-loading", PAGE)
        self.assertIn("is-loadingmore", PAGE)

    def test_loading_helpers_and_wrapper_present(self):
        self.assertIn("const setLoading =", PAGE)
        self.assertIn("const setLoadingMore =", PAGE)
        # reload() wraps the render and clears in finally (fail-safe).
        self.assertIn("async function reloadInner(", PAGE)
        self.assertIn("finally { setLoading(false); }", PAGE)
        self.assertIn("finally { loading = false; setLoadingMore(false); }", PAGE)

    def test_unified_pager_indirection_present(self):
        # One loadMore() drives every browse panel via loadNextPage; no per-view scroll wiring.
        self.assertIn("let loadNextPage = null;", PAGE)
        self.assertIn("if (loading || exhausted || !loadNextPage) return;", PAGE)
        self.assertIn("loadNextPage = loadFactsPage;", PAGE)


if __name__ == "__main__":
    unittest.main()
