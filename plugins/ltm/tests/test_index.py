"""Code/docs index tests — chunking, chunk store, indexer, and index recall.

Uses the dependency-free HashEmbedding, so no fastembed venv is required. The indexer
and recall paths run against a real temp directory of markdown so freshness — which
re-reads the live file — is exercised for real.

Run: python3 -m unittest discover -s plugins/ltm/tests
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bin"))

from core.config import get_config  # noqa: E402
from core.index import treesitter_symbols  # noqa: E402
from core.index.chunking import make_slug, split_markdown  # noqa: E402
from core.index.code_symbols import extract_code_symbols, extract_symbols  # noqa: E402
from core.index.index_recall import get_chunk, get_outline, search_index  # noqa: E402
from core.index.indexer import index_file, index_project, tree_signature  # noqa: E402
from core.ports.embedding import HashEmbedding  # noqa: E402
from core.store import _SCHEMA_VERSION, Store  # noqa: E402


class TreeSignatureTests(unittest.TestCase):
    def test_signature_stable_and_reacts_to_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("alpha")
            (root / "b.py").write_text("x = 1\n")
            sig = tree_signature(root)
            self.assertEqual(sig, tree_signature(root))  # stable when nothing changes
            self.assertGreaterEqual(sig, 0)

            (root / "c.md").write_text("gamma")  # add a file
            added = tree_signature(root)
            self.assertNotEqual(added, sig)

            (root / "a.md").write_text("alpha plus more content")  # edit (size changes)
            self.assertNotEqual(tree_signature(root), added)

            (root / "c.md").unlink()  # remove a file
            self.assertNotEqual(tree_signature(root), added)

    def test_signature_ignores_non_indexed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("alpha")
            before = tree_signature(root)
            (root / "notes.bin").write_text("binary junk")  # not an indexed extension
            self.assertEqual(tree_signature(root), before)


class ChunkingTests(unittest.TestCase):
    def test_atx_hierarchy_and_slugs(self):
        secs = split_markdown("# Setup\nx\n\n## Database\ny\n\n### Ports\nz\n", "doc")
        by_title = {s.title: s for s in secs}
        self.assertEqual(by_title["Database"].slug, "setup/database")
        self.assertEqual(by_title["Ports"].slug, "setup/database/ports")
        self.assertEqual(by_title["Ports"].heading_path, "Setup › Database › Ports")

    def test_frontmatter_stripped_from_root(self):
        secs = split_markdown("---\ntitle: T\n---\nlead prose\n\n# H\nbody\n", "doc")
        root = secs[0]
        self.assertEqual(root.level, 0)
        self.assertEqual(root.body, "lead prose")

    def test_fenced_hash_comment_is_not_a_heading(self):
        secs = split_markdown("# Real\n```bash\n# not a heading\n```\n", "doc")
        self.assertEqual([s.title for s in secs], ["Real"])

    def test_setext_heading_detected(self):
        secs = split_markdown("Title Here\n=====\nbody\n", "doc")
        self.assertTrue(any(s.title == "Title Here" and s.level == 1 for s in secs))

    def test_byte_offsets_reproduce_body(self):
        text = "# A\nalpha\n\n## B\nbeta\n"
        cb = text.encode("utf-8")
        for s in split_markdown(text, "doc"):
            self.assertEqual(cb[s.byte_start : s.byte_end].decode("utf-8", "ignore").strip(), s.body)

    def test_headingless_file_is_one_root(self):
        secs = split_markdown("just prose, no headings\n", "readme")
        self.assertEqual(len(secs), 1)
        self.assertEqual(secs[0].title, "readme")

    def test_sibling_slug_collision_disambiguated(self):
        secs = split_markdown("## Notes\na\n\n## Notes\nb\n", "doc")
        slugs = [s.slug for s in secs if s.level == 2]
        self.assertEqual(len(set(slugs)), 2)

    def test_make_slug_normalises(self):
        self.assertEqual(make_slug("Hello, World!"), "hello-world")


class ChunkStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.pk = "proj"

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()

    def _chunk(self, anchor: str, title: str, body: str, summary: str = "") -> dict:
        return {
            "id": self.store.chunk_id(self.pk, "d.md", anchor),
            "anchor": anchor,
            "title": title,
            "heading_path": title,
            "level": 1,
            "summary": summary,
            "body": body,
            "byte_start": 0,
            "byte_end": len(body),
            "content_hash": "h",
            "dim": 8,
            "scale": 1.0,
            "vec_int8": b"\x00" * 8,
        }

    def test_migration_created_chunk_tables(self):
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], _SCHEMA_VERSION)
        names = {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master")}
        self.assertTrue({"chunks", "chunk_sources", "chunks_fts", "index_meta"} <= names)

    def test_chunk_projects_uses_index_meta_label(self):
        # An index-only project (chunks, no memory facts) must show a real name.
        self.store.replace_source_chunks(self.pk, "d.md", [self._chunk("a", "A", "x")], "h", 1)
        row = next(r for r in self.store.chunk_projects() if r["project_key"] == self.pk)
        self.assertIsNone(row["label"])  # no meta yet → viewer falls back to the key
        self.store.set_index_meta({"key": self.pk, "label": "my-project", "path": "/tmp/my-project"})
        row = next(r for r in self.store.chunk_projects() if r["project_key"] == self.pk)
        self.assertEqual(row["label"], "my-project")
        self.assertEqual(row["path"], "/tmp/my-project")

    def test_set_index_meta_upserts(self):
        self.store.set_index_meta({"key": self.pk, "label": "a", "path": "/a"})
        self.store.set_index_meta({"key": self.pk, "label": "b", "path": "/b"})
        row = self.store.db.execute("SELECT label, path FROM index_meta WHERE project_key = ?", (self.pk,)).fetchone()
        self.assertEqual((row["label"], row["path"]), ("b", "/b"))

    def test_replace_and_fetch(self):
        self.store.replace_source_chunks(self.pk, "d.md", [self._chunk("intro", "Intro", "hello postgres")], "fh", 1)
        row = self.store.get_chunk(self.pk, "intro")
        self.assertEqual(row["title"], "Intro")
        self.assertEqual(self.store.chunk_count(self.pk), 1)
        self.assertEqual(self.store.source_state(self.pk, "d.md"), ("fh", 1))

    def test_replace_swaps_out_removed_sections(self):
        self.store.replace_source_chunks(
            self.pk, "d.md", [self._chunk("a", "A", "x"), self._chunk("b", "B", "y")], "h1", 1
        )
        self.store.replace_source_chunks(self.pk, "d.md", [self._chunk("a", "A", "x")], "h2", 2)
        self.assertEqual(self.store.chunk_count(self.pk), 1)
        self.assertIsNone(self.store.get_chunk(self.pk, "b"))

    def test_fts_search_matches_body_term(self):
        self.store.replace_source_chunks(
            self.pk, "d.md", [self._chunk("db", "Database", "uses postgres on 5432")], "h", 1
        )
        self.assertIn(self.store.chunk_id(self.pk, "d.md", "db"), self.store.chunk_fts_search(self.pk, "postgres"))

    def test_delete_source(self):
        self.store.replace_source_chunks(self.pk, "d.md", [self._chunk("a", "A", "x")], "h", 1)
        self.store.delete_source(self.pk, "d.md")
        self.assertEqual(self.store.chunk_count(self.pk), 0)
        self.assertNotIn("d.md", self.store.indexed_sources(self.pk))

    def test_prune_chunks(self):
        self.store.replace_source_chunks(self.pk, "d.md", [self._chunk("a", "A", "x")], "h", 1)
        self.store.prune_chunks(self.pk)
        self.assertEqual(self.store.chunk_count(self.pk), 0)


class IndexerAndRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.repo = tempfile.TemporaryDirectory()
        self.project = {"key": "p", "path": self.repo.name, "label": "p"}
        self._write(
            "guide.md",
            "# Setup\nInstall it.\n\n## Database\nUse Postgres on port 5432.\n\n## Auth\nHeader based only.\n",
        )

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()
        self.repo.cleanup()

    def _write(self, rel: str, text: str) -> None:
        path = Path(self.repo.name) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _index(self) -> dict:
        return index_project(self.store, self.embedder, self.cfg, self.project, self.repo.name)

    def test_index_creates_chunks(self):
        stats = self._index()
        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["chunks"], 3)

    def test_reindex_short_circuits_unchanged(self):
        self._index()
        stats = self._index()
        self.assertEqual(stats["files"], 0)
        self.assertEqual(stats["skipped"], 1)

    def test_edited_file_reindexes(self):
        self._index()
        self._write("guide.md", "# Setup\nInstall it.\n\n## Database\nUse Postgres on port 5432 now clustered.\n")
        stats = self._index()
        self.assertEqual(stats["files"], 1)

    def test_deleted_file_pruned(self):
        self._write("extra.md", "# Extra\ngone soon\n")
        self._index()
        os.unlink(Path(self.repo.name) / "extra.md")
        stats = self._index()
        self.assertEqual(stats["deleted"], 1)
        self.assertIsNone(self.store.get_chunk(self.project["key"], "extra"))

    def test_search_returns_outline_rows(self):
        self._index()
        res = search_index(self.store, self.embedder, self.cfg, self.project, "postgres port", k=3)
        self.assertTrue(res["results"])
        top = res["results"][0]
        self.assertIn("anchor", top)
        self.assertNotIn("body", top)  # search never returns bodies
        self.assertEqual(top["freshness"], "fresh")

    def test_search_diversity_cap_limits_one_file(self):
        # Many sections in one file — the per-file cap keeps the result set from being flooded.
        big = "# Root\n" + "".join(f"## S{i}\npostgres text {i}\n\n" for i in range(10))
        self._write("big.md", big)
        self._index()
        res = search_index(self.store, self.embedder, self.cfg, self.project, "postgres", k=20)
        from collections import Counter

        per_file = Counter(r["source_path"] for r in res["results"])
        self.assertLessEqual(per_file["big.md"], 3)

    def test_get_chunk_freshness_transitions(self):
        self._index()
        self.assertEqual(get_chunk(self.store, self.project, "setup/database")["freshness"], "fresh")
        # edit only the Auth section: database stays fresh, auth becomes edited
        self._write(
            "guide.md",
            "# Setup\nInstall it.\n\n## Database\nUse Postgres on port 5432.\n\n## Auth\nHeader based only. Plus tokens.\n",
        )
        self.assertEqual(get_chunk(self.store, self.project, "setup/database")["freshness"], "fresh")
        self.assertEqual(get_chunk(self.store, self.project, "setup/auth")["freshness"], "edited")
        # remove the heading entirely: anchor is now stale
        self._write("guide.md", "# Setup\nInstall it.\n")
        self.assertEqual(get_chunk(self.store, self.project, "setup/database")["freshness"], "stale")

    def test_get_chunk_missing_ref(self):
        self._index()
        self.assertFalse(get_chunk(self.store, self.project, "no/such/anchor")["found"])

    def test_outline_has_no_bodies(self):
        self._index()
        outline = get_outline(self.store, self.project)
        self.assertEqual(outline["count"], 3)
        self.assertTrue(all("body" not in s for s in outline["sections"]))

    def test_index_file_lifecycle(self):
        py = Path(self.repo.name) / "svc.py"
        py.write_text("def handler(x):\n    return x\n", encoding="utf-8")
        self.assertEqual(index_file(self.store, self.embedder, self.cfg, self.project, str(py))["status"], "indexed")
        self.assertEqual(index_file(self.store, self.embedder, self.cfg, self.project, str(py))["status"], "skipped")
        py.write_text("def handler(x):\n    return x\ndef helper():\n    return 1\n", encoding="utf-8")
        index_file(self.store, self.embedder, self.cfg, self.project, str(py))
        self.assertEqual(
            {s["anchor"] for s in self.store.chunk_outline(self.project["key"], "svc.py")}, {"handler", "helper"}
        )
        py.unlink()
        self.assertEqual(index_file(self.store, self.embedder, self.cfg, self.project, str(py))["status"], "removed")
        self.assertEqual(self.store.chunk_outline(self.project["key"], "svc.py"), [])


class CodeSymbolTests(unittest.TestCase):
    SRC = (
        "import os\n\n"
        "def top_level(a, b: int = 3) -> str:\n"
        '    """Does a thing."""\n'
        "    return str(a)\n\n"
        "class Widget:\n"
        "    def method(self, x):\n"
        "        return x\n\n"
        "    async def afetch(self):\n"
        "        return 1\n"
    )

    def test_extracts_functions_classes_methods(self):
        syms = {s.qualname: s for s in extract_symbols(self.SRC, "m")}
        self.assertEqual(syms["top_level"].kind, "function")
        self.assertEqual(syms["Widget"].kind, "class")
        self.assertEqual(syms["Widget.method"].kind, "method")
        self.assertEqual(syms["Widget.afetch"].kind, "async_method")

    def test_signature_and_docstring(self):
        syms = {s.qualname: s for s in extract_symbols(self.SRC, "m")}
        self.assertEqual(syms["top_level"].signature, "def top_level(a, b: int=3) -> str:")
        self.assertEqual(syms["top_level"].docstring, "Does a thing.")

    def test_byte_spans_reproduce_body(self):
        cb = self.SRC.encode("utf-8")
        for s in extract_symbols(self.SRC, "m"):
            self.assertEqual(cb[s.byte_start : s.byte_end].decode("utf-8", "ignore").rstrip(), s.body)

    def test_syntax_error_yields_nothing(self):
        self.assertEqual(extract_symbols("def broken(:\n", "m"), [])


class CodeIndexingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.repo = tempfile.TemporaryDirectory()
        self.project = {"key": "p", "path": self.repo.name, "label": "p"}
        self._write(
            "app.py", "def connect_db(url):\n    return url\n\nclass Server:\n    def start(self):\n        return 1\n"
        )
        self._write("readme.md", "# Overview\nProse about the server.\n")

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()
        self.repo.cleanup()

    def _write(self, rel: str, text: str) -> None:
        (Path(self.repo.name) / rel).write_text(text, encoding="utf-8")

    def test_indexes_both_code_and_docs(self):
        stats = index_project(self.store, self.embedder, self.cfg, self.project, self.repo.name)
        self.assertEqual(stats["files"], 2)
        # 3 code symbols (connect_db, Server, Server.start) + 1 doc section
        self.assertEqual(stats["chunks"], 4)

    def test_search_code_isolates_code_symbols(self):
        index_project(self.store, self.embedder, self.cfg, self.project, self.repo.name)
        res = search_index(self.store, self.embedder, self.cfg, self.project, "connect db", k=5, kind="code_symbol")
        self.assertTrue(res["results"])
        self.assertTrue(all(r["kind"] == "code_symbol" for r in res["results"]))

    def test_search_docs_excludes_code(self):
        index_project(self.store, self.embedder, self.cfg, self.project, self.repo.name)
        res = search_index(self.store, self.embedder, self.cfg, self.project, "server", k=5, kind="doc_section")
        self.assertTrue(all(r["kind"] == "doc_section" for r in res["results"]))

    def test_get_symbol_full_source_and_freshness(self):
        index_project(self.store, self.embedder, self.cfg, self.project, self.repo.name)
        c = get_chunk(self.store, self.project, "Server.start")
        self.assertTrue(c["found"])
        self.assertIn("def start", c["body"])
        self.assertEqual(c["freshness"], "fresh")
        # edit the symbol -> edited
        self._write(
            "app.py", "def connect_db(url):\n    return url\n\nclass Server:\n    def start(self):\n        return 2\n"
        )
        self.assertEqual(get_chunk(self.store, self.project, "Server.start")["freshness"], "edited")

    def test_code_outline_scoped(self):
        index_project(self.store, self.embedder, self.cfg, self.project, self.repo.name)
        outline = get_outline(self.store, self.project, kind="code_symbol")
        anchors = {s["anchor"] for s in outline["sections"]}
        self.assertEqual(anchors, {"connect_db", "Server", "Server.start"})


def _has_treesitter() -> bool:
    return treesitter_symbols.extract_symbols("def f(): pass\n", ".py") is not None


class DispatcherFallbackTests(unittest.TestCase):
    def test_python_dispatch_returns_symbols(self):
        syms = {
            s.qualname for s in extract_code_symbols("def a():\n    pass\nclass B:\n    def c(self): pass\n", ".py")
        }
        self.assertEqual(syms, {"a", "B", "B.c"})

    def test_unsupported_extension_returns_empty(self):
        self.assertEqual(extract_code_symbols("SELECT 1;", ".sql"), [])


@unittest.skipUnless(_has_treesitter(), "tree-sitter not provisioned")
class TreeSitterTests(unittest.TestCase):
    def test_typescript_symbols(self):
        src = (
            "export function greet(n: string): string { return n; }\n"
            "export const Widget = (p: Props) => null;\n"
            "class Server {\n  start(): void {}\n  async stop() {}\n}\n"
            "interface Config { port: number; }\n"
            "type Id = string;\n"
        )
        syms = {s.qualname: s for s in treesitter_symbols.extract_symbols(src, ".tsx")}
        self.assertEqual(syms["greet"].kind, "function")
        self.assertEqual(syms["Widget"].kind, "function")  # const arrow
        self.assertEqual(syms["Server.start"].kind, "method")
        self.assertEqual(syms["Server.stop"].kind, "method")
        self.assertEqual(syms["Config"].kind, "interface")
        self.assertEqual(syms["Id"].kind, "type")

    def test_ts_byte_spans_reproduce_body(self):
        src = "function f(x: number): number {\n  return x;\n}\n"
        cb = src.encode("utf-8")
        for s in treesitter_symbols.extract_symbols(src, ".ts"):
            self.assertEqual(cb[s.byte_start : s.byte_end].decode("utf-8", "ignore").rstrip(), s.body)

    def test_python_via_treesitter_matches_ast_qualnames(self):
        src = "def top(a):\n    return a\nclass C:\n    def m(self): pass\n"
        ts_names = {s.qualname for s in treesitter_symbols.extract_symbols(src, ".py")}
        ast_names = {s.qualname for s in extract_symbols(src, "")}
        self.assertEqual(ts_names, ast_names)

    def test_malformed_source_returns_empty_not_none(self):
        self.assertEqual(treesitter_symbols.extract_symbols("function (", ".ts"), [])


@unittest.skipUnless(_has_treesitter(), "tree-sitter not provisioned")
class TypeScriptIndexingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["LTM_DATA_DIR"] = self.tmp.name
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.repo = tempfile.TemporaryDirectory()
        self.project = {"key": "p", "path": self.repo.name, "label": "p"}
        (Path(self.repo.name) / "widget.tsx").write_text(
            "export const Button = (props: Props) => {\n  return null;\n};\n"
            "export function connect(url: string): void {}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.store.close()
        os.environ.pop("LTM_DATA_DIR", None)
        self.tmp.cleanup()
        self.repo.cleanup()

    def test_index_and_search_tsx(self):
        index_project(self.store, self.embedder, self.cfg, self.project, self.repo.name)
        res = search_index(self.store, self.embedder, self.cfg, self.project, "connect url", k=5, kind="code_symbol")
        anchors = {r["anchor"] for r in res["results"]}
        self.assertIn("connect", anchors)
        c = get_chunk(self.store, self.project, "Button")
        self.assertTrue(c["found"])
        self.assertEqual(c["freshness"], "fresh")


if __name__ == "__main__":
    unittest.main()
