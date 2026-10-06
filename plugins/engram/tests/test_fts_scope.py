"""The keyword (FTS) channels and chunk lookup scope before they rank or read (``core/store.py``).

``Store.fts_search`` / ``chunk_fts_search`` filter the store-wide FTS ``MATCH`` to the project's
rowids before bm25 and before any content row is read; ``Store.get_chunk`` looks an id up by
primary key before falling back to the anchor. Both are pure speed-ups, so every result here is
checked against the old SQL (kept below as the oracle), and the plan-shape tests pin the query
shape the speed depends on — including the load-bearing unary ``+``.
"""

from __future__ import annotations

import unittest

from _harness import temp_data_dir

from core.store import Store, _chunk_scope, _fts_match_expr

# The SQL the scoped queries replaced: join every store-wide match, then filter. The oracle.
OLD_FACTS_SQL = (
    "SELECT f.id FROM facts_fts JOIN facts f ON f.rowid = facts_fts.rowid "
    "WHERE facts_fts MATCH ? AND f.project_key = ? AND f.status = 'active' "
    "ORDER BY bm25(facts_fts) LIMIT ?"
)
OLD_CHUNKS_SQL = (
    "SELECT c.id FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
    "WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts, 3.0, 2.0, 1.5, 1.0) LIMIT ?"
)
OLD_GET_CHUNK_SQL = "SELECT * FROM chunks WHERE project_key = ? AND (id = ? OR anchor = ?) LIMIT 1"

PROJECTS = ("alpha", "beta", "__global__")
QUERIES = [
    "deploy",
    "deploy pipeline lambda",
    "deploying",  # porter stems to deploy
    "postgres",
    "widget.py",  # a `files` / source-path term
    "deploy-pipeline! (lambda)",  # FTS5 operator characters are quoted away
    "nothing matches this",
    "",  # no tokens → no query at all
]
LIMITS = (1, 3, 12, 50, 1000)


def _fact_texts(project: str) -> list[tuple[str, dict]]:
    """A spread of bm25 scores across the searchable columns, plus exact ties (same length, the
    term once) so a ``LIMIT`` cuts through a run of equal scores."""
    rows = [
        ("deploy pipeline runs on GitHub Actions", {}),
        ("we deployed to AWS Lambda last week", {}),
        ("deploying the deploy pipeline deploys twice", {}),
        ("postgres holds the ledger", {"title": "database"}),
        ("an unrelated note about lunch", {"narrative": "deploy came up in passing"}),
        ("the widget renders the outline", {"files": ["src/widget.py"]}),
    ]
    rows += [(f"deploy tie {project} {i:02d}", {}) for i in range(8)]
    return [(f"{text} ({project})", extra) for text, extra in rows]


def _chunk(store: Store, project: str, source: str, anchor: str, kind: str, title: str, body: str) -> dict:
    return {
        "id": store.chunk_id(project, source, anchor),
        "kind": kind,
        "anchor": anchor,
        "title": title,
        "heading_path": title,
        "level": 1,
        "summary": "",
        "body": body,
        "byte_start": 0,
        "byte_end": len(body),
        "content_hash": "h",
        "dim": 8,
        "scale": 1.0,
        "vec_int8": b"\x00" * 8,
    }


class _ScopedFixture(unittest.TestCase):
    """Three projects' facts (some archived) and two projects' chunks, interleaved by rowid."""

    def setUp(self):
        tmp = temp_data_dir(self)
        self.store = Store(f"{tmp.name}/fts.db")
        self.addCleanup(self.store.close)
        by_project = {p: _fact_texts(p) for p in PROJECTS}
        for i in range(max(map(len, by_project.values()))):  # interleave, so scopes share rowid ranges
            for project, texts in by_project.items():
                if i < len(texts):
                    text, extra = texts[i]
                    self.store.add(
                        project={"key": project, "label": project, "path": "/x"},
                        session_id="s",
                        kind="fact",
                        text=text,
                        vec_int8=b"\x00" * 8,
                        scale=1.0,
                        dim=8,
                        vec_bits=b"",
                        importance=0.5,
                        **extra,
                    )
        archived = [self.store.fact_id("alpha", text) for text, _ in by_project["alpha"][1:3]]
        self.store.set_status(archived[:1], "superseded")
        self.store.set_status(archived[1:], "archived")
        for project in ("alpha", "beta"):
            for source, kind in (("src/widget.py", "code_symbol"), ("docs/deploy.md", "doc_section")):
                chunks = [
                    _chunk(self.store, project, source, "Deploy.run", kind, "Deploy run", "deploy the lambda pipeline"),
                    _chunk(self.store, project, source, f"{source}#db", kind, "Database", "postgres on 5432"),
                    *(
                        _chunk(self.store, project, source, f"tie{i}", kind, "Tie", f"deploy tie {i:02d}")
                        for i in range(5)
                    ),
                ]
                self.store.replace_source_chunks(project, source, chunks, "h", 1)


class ScopedFtsParityTests(_ScopedFixture):
    def test_fact_search_matches_the_old_sql(self):
        for project in PROJECTS:
            for query in QUERIES:
                for limit in LIMITS:
                    with self.subTest(project=project, query=query, limit=limit):
                        match = _fts_match_expr(query)
                        old = (
                            [r[0] for r in self.store.db.execute(OLD_FACTS_SQL, (match, project, limit))]
                            if match
                            else []
                        )
                        self.assertEqual(self.store.fts_search(project, query, limit=limit), old)

    def test_chunk_search_matches_the_old_sql_in_every_scope(self):
        scopes = [
            (None, None),
            ("code_symbol", None),
            ("doc_section", None),
            (None, "docs/deploy.md"),
            ("code_symbol", "src/widget.py"),
            ("doc_section", "src/widget.py"),  # an empty scope
        ]
        for project in ("alpha", "beta", "nobody"):
            for kind, source in scopes:
                where, params = _chunk_scope(project, kind, source)
                old_where = " AND ".join(f"c.{clause}" for clause in where.split(" AND "))
                for query in QUERIES:
                    for limit in LIMITS:
                        with self.subTest(project=project, kind=kind, source=source, query=query, limit=limit):
                            match = _fts_match_expr(query)
                            sql = OLD_CHUNKS_SQL.format(where=old_where)
                            old = [r[0] for r in self.store.db.execute(sql, [match, *params, limit])] if match else []
                            new = self.store.chunk_fts_search(
                                project, query, limit=limit, kind=kind, source_path=source
                            )
                            self.assertEqual(new, old)

    def test_the_fixture_exercises_ties_inactive_rows_and_other_projects(self):
        # Guard the oracle's reach: ties cut by a LIMIT, archived rows and foreign matches all occur.
        match = _fts_match_expr("deploy")
        scores = [
            r[0]
            for r in self.store.db.execute("SELECT bm25(facts_fts) FROM facts_fts WHERE facts_fts MATCH ?", (match,))
        ]
        self.assertGreater(len(scores) - len(set(scores)), 5)
        statuses = {r[0] for r in self.store.db.execute("SELECT status FROM facts WHERE project_key = 'alpha'")}
        self.assertEqual(statuses, {"active", "superseded", "archived"})
        self.assertLess(len(self.store.fts_search("alpha", "deploy", limit=1000)), len(scores))


class ScopedFtsPlanTests(_ScopedFixture):
    """The query shape the speed-up depends on. Each assertion is checked against the shapes it
    must reject, so a "tidied" query can't slip through."""

    def _traced(self, call) -> str:
        statements: list[str] = []
        self.store.db.set_trace_callback(statements.append)
        try:
            call()
        finally:
            self.store.db.set_trace_callback(None)
        (statement,) = [s for s in statements if "MATCH" in s]
        return statement

    def _plan(self, sql: str) -> list[tuple[int, int, str]]:
        return [(row[0], row[1], row[3]) for row in self.store.db.execute("EXPLAIN QUERY PLAN " + sql)]

    def assert_scoped(self, plan: list[tuple[int, int, str]], fts_table: str, scope_index: str) -> None:
        detail = {node: text for node, _parent, text in plan}
        (fts,) = [(node, parent, text) for node, parent, text in plan if text.startswith(f"SCAN {fts_table} ")]
        # (b) the FTS scan is not a per-rowid seek — FTS5's index string carries no `=`
        self.assertNotIn("=", fts[2].split("INDEX", 1)[1], f"FTS seeks by rowid: {fts[2]}")
        # (a) the scope is a rowid list, read from the scope's index
        lists = [node for node, _parent, text in plan if text.startswith("LIST SUBQUERY")]
        self.assertEqual(len(lists), 1, "no scope prefilter")
        self.assertTrue(
            any(parent == lists[0] and scope_index in text for _node, parent, text in plan),
            f"the scope list does not read {scope_index}",
        )
        # (c) ranking happens inside the CTE; content rows are read only after its LIMIT
        self.assertRegex(detail.get(fts[1], ""), r"^(CO-ROUTINE|MATERIALIZE) hit$")
        rowid_reads = [(parent, text) for _node, parent, text in plan if "INTEGER PRIMARY KEY (rowid=?)" in text]
        self.assertEqual([parent for parent, _text in rowid_reads], [0], "content rows read inside the MATCH loop")

    def test_fact_search_scopes_before_it_ranks(self):
        sql = self._traced(lambda: self.store.fts_search("alpha", "deploy pipeline"))
        self.assert_scoped(self._plan(sql), "facts_fts", "COVERING INDEX idx_facts_project")

    def test_chunk_search_scopes_before_it_ranks(self):
        for kind, source, index in (
            (None, None, "COVERING INDEX idx_chunks_project"),  # the narrowest: the hook's prefilter
            ("code_symbol", None, "COVERING INDEX idx_chunks_kind"),
            (None, "docs/deploy.md", "COVERING INDEX idx_chunks_source"),
        ):
            with self.subTest(kind=kind, source=source):
                sql = self._traced(
                    lambda: self.store.chunk_fts_search("alpha", "deploy", kind=kind, source_path=source)
                )
                self.assert_scoped(self._plan(sql), "chunks_fts", index)

    def test_the_plan_check_rejects_the_old_and_the_unguarded_shapes(self):
        match = _fts_match_expr("deploy pipeline")
        old = self._traced(lambda: self.store.db.execute(OLD_FACTS_SQL, (match, "alpha", 50)).fetchall())
        scoped = self._traced(lambda: self.store.fts_search("alpha", "deploy pipeline"))
        unguarded = scoped.replace("+rowid IN", "rowid IN")
        self.assertNotEqual(unguarded, scoped)
        for name, sql in (("old join-then-filter", old), ("without the unary +", unguarded)):
            with self.subTest(shape=name), self.assertRaises(AssertionError):
                self.assert_scoped(self._plan(sql), "facts_fts", "COVERING INDEX idx_facts_project")


class GetChunkTests(_ScopedFixture):
    def _old(self, project: str, ref: str):
        return self.store.db.execute(OLD_GET_CHUNK_SQL, (project, ref, ref)).fetchone()

    def test_ids_and_anchors_fetch_what_the_old_sql_fetched(self):
        refs = [r[0] for r in self.store.db.execute("SELECT id FROM chunks")]
        refs += [r[0] for r in self.store.db.execute("SELECT DISTINCT anchor FROM chunks")]
        refs += ["no/such/anchor", ""]
        for project in ("alpha", "beta", "nobody"):
            for ref in refs:
                with self.subTest(project=project, ref=ref):
                    old, new = self._old(project, ref), self.store.get_chunk(project, ref)
                    self.assertEqual(None if new is None else tuple(new), None if old is None else tuple(old))

    def test_a_shared_anchor_resolves_to_the_first_indexed_chunk(self):
        rows = self.store.db.execute(
            "SELECT rowid, id FROM chunks WHERE project_key = 'alpha' AND anchor = 'Deploy.run' ORDER BY rowid"
        ).fetchall()
        self.assertEqual(len(rows), 2)  # one per source file
        self.assertEqual(self.store.get_chunk("alpha", "Deploy.run")["id"], rows[0]["id"])

    def test_an_id_is_fetched_by_primary_key(self):
        chunk_id = self.store.chunk_id("alpha", "src/widget.py", "Deploy.run")
        statements: list[str] = []
        self.store.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(self.store.get_chunk("alpha", chunk_id)["id"], chunk_id)
        finally:
            self.store.db.set_trace_callback(None)
        (statement,) = statements  # found by id: the anchor fallback never runs
        plan = [row[3] for row in self.store.db.execute("EXPLAIN QUERY PLAN " + statement)]
        self.assertEqual(plan, ["SEARCH chunks USING INDEX sqlite_autoindex_chunks_1 (id=?)"])

    def test_an_anchor_is_fetched_through_its_index_in_rowid_order(self):
        statements: list[str] = []
        self.store.db.set_trace_callback(statements.append)
        try:
            self.assertIsNotNone(self.store.get_chunk("alpha", "Deploy.run"))
        finally:
            self.store.db.set_trace_callback(None)
        fallback = statements[-1]  # the id lookup missed; the anchor lookup answered
        plan = [row[3] for row in self.store.db.execute("EXPLAIN QUERY PLAN " + fallback)]
        self.assertEqual(plan, ["SEARCH chunks USING INDEX idx_chunks_anchor (project_key=? AND anchor=?)"])

    def test_an_id_from_another_project_is_not_returned(self):
        self.assertIsNone(self.store.get_chunk("beta", self.store.chunk_id("alpha", "src/widget.py", "Deploy.run")))


if __name__ == "__main__":
    unittest.main()
