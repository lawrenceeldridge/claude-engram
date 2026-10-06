"""No consolidation stage issues SQL per fact (#66 part 3).

A per-fact query is invisible at a few hundred facts and catastrophic at 10⁵: ``refine``'s
per-fact ``supersede_count`` (a full-table scan each) made one pass over a 144k-fact store take
~13.5 h. Each stage of ``consolidation.stages`` — the same table ``consolidate`` runs — is run
with its gate open on a small and a 4× larger store; the statements it issues must not grow with
the store.
"""

from __future__ import annotations

import random
import unittest
from dataclasses import replace
from unittest import mock

from _harness import temp_data_dir

from core import consolidation, service
from core.config import get_config
from core.ports.distill import DistilledFact
from core.ports.embedding import HashEmbedding
from core.store import Store

SMALL, LARGE = 40, 160
_VOCAB = [f"w{i}" for i in range(4000)]


def _statements_per_stage(n_facts: int) -> dict[str, int]:
    """Statements each stage issues on a fresh store of ``n_facts`` distinct facts, gates open."""
    cfg = replace(
        get_config(),
        distiller="heuristic",
        stm_capacity=10**6,  # displace runs; nothing overflows
        stm_max_age_days=10**6,  # mature runs; nothing is that old
        integrate_threshold=0.99,  # integrate runs; distinct texts don't cluster
        refine_keep_max=10**6,  # refine runs (the #67 stage); keeps everything
        purge_horizon_days=10**6,  # purge runs; nothing is that cold
        episodic_ttl_days=10**6,
        episodic_max_chunks=10**6,
    )
    store = Store(cfg.db_path)
    try:
        project = {"key": f"p{n_facts}", "path": "/tmp/p", "label": "p"}
        rng = random.Random(n_facts)
        records = [(DistilledFact(" ".join(rng.sample(_VOCAB, 8))), 1000.0 + i) for i in range(n_facts)]
        embedder = HashEmbedding(dim=cfg.dim)
        service.bulk_add_records(store, embedder, cfg, project, "s1", records)
        counts: dict[str, int] = {}
        for key, run in consolidation.stages(store, cfg, project, now=10**6, embedder=embedder):
            issued = []
            store.db.set_trace_callback(issued.append)
            try:
                run()
            finally:
                store.db.set_trace_callback(None)
            counts[key] = len(issued)
        return counts
    finally:
        store.close()


class ConsolidationScaleTests(unittest.TestCase):
    def setUp(self):
        temp_data_dir(self)

    def test_no_stage_issues_sql_per_fact(self):
        small, large = _statements_per_stage(SMALL), _statements_per_stage(LARGE)
        self.assertEqual(set(small), {key for key, _run in consolidation.stages(None, get_config(), {"key": "x"})})
        for stage, issued in large.items():
            with self.subTest(stage=stage, small=small[stage], large=issued):
                # 4× the facts must not mean more statements: a per-fact query would add ~120.
                self.assertLessEqual(issued, small[stage] + 2)

    def test_the_guard_catches_a_per_fact_stage(self):
        """Negative control: a stage that queries once per fact (the old ``refine``) must show."""
        real = consolidation.stages

        def with_a_per_fact_stage(store, cfg, project, now=None, embedder=None):
            def per_fact() -> int:
                for row in store.active_rows_for_project(project["key"]):
                    store.db.execute("SELECT COUNT(*) FROM facts WHERE superseded_by = ?", (row["id"],)).fetchone()
                return 0

            return (*real(store, cfg, project, now, embedder), ("per_fact", per_fact))

        with mock.patch.object(consolidation, "stages", with_a_per_fact_stage):
            small, large = _statements_per_stage(SMALL), _statements_per_stage(LARGE)
        self.assertGreater(large["per_fact"], small["per_fact"] + 2)


if __name__ == "__main__":
    unittest.main()
