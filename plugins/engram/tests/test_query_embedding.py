"""Read paths embed their query through ``embed_query``, once per prompt (``core/ports/embedding``).

The port's contract: every read path embeds its query with ``embed_query``; stored text (facts,
chunks) goes through ``embed`` / ``embed_one``. The prompt hook's two blocks (memory, index) share
one embedding of the prompt through ``QueryMemo``, an ``EmbeddingGateway`` in front of the real one
for that call only.
"""

from __future__ import annotations

import io
import json
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest import mock

from _harness import temp_data_dir

import recall_prompt

from core import service
from core.config import get_config
from core.index.index_recall import search_index
from core.index.indexer import index_project
from core.ports.embedding import EmbeddingGateway, HashEmbedding, QueryMemo
from core.service import PROMPT_INDEX_HEADER, PROMPT_MEMORY_HEADER
from core.store import Store

FACTS = [
    "The deploy pipeline ships to AWS Lambda through GitHub Actions.",
    "Signing keys rotate every ninety days.",
]
SOURCES = {
    "deploy.py": 'def ship_lambda():\n    """Ships the deploy pipeline to AWS Lambda."""\n',
    "keys.py": 'def rotate_keys():\n    """Rotates the signing keys for the deploy pipeline."""\n',
}
PROMPT = "how does the deploy pipeline ship to aws lambda"


class Recording(EmbeddingGateway):
    """A hash gateway that records which method embedded which text. Each method computes on its
    own (never through another), so a count is exactly the calls a read path made."""

    semantic = False

    def __init__(self, dim: int, query_as: dict[str, str] | None = None) -> None:
        self.dim = dim
        self._hash = HashEmbedding(dim=dim)
        self._query_as = query_as or {}
        self.calls: Counter[tuple[str, str]] = Counter()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.update(("embed", text) for text in texts)
        return self._hash.embed(texts)

    def embed_one(self, text: str) -> list[float]:
        self.calls["embed_one", text] += 1
        return self._hash.embed([text])[0]

    def embed_query(self, text: str) -> list[float]:
        # ``query_as`` makes the gateway asymmetric: a query embeds as some other text.
        self.calls["embed_query", text] += 1
        return self._hash.embed([self._query_as.get(text, text)])[0]

    def methods(self) -> Counter[str]:
        totals: Counter[str] = Counter()
        for (method, _text), n in self.calls.items():
            totals[method] += n
        return totals


class _Fixture(unittest.TestCase):
    def setUp(self):
        tmp = Path(temp_data_dir(self).name)
        self.cfg = replace(get_config(), distiller="heuristic", index_min_sim=-1.0, review_enabled=True)
        self.store = Store(tmp / "memory.db")
        self.addCleanup(self.store.close)
        self.project = {"key": "proj", "path": str(tmp), "label": "proj"}
        hashing = HashEmbedding(dim=self.cfg.dim)
        service.add_facts(self.store, hashing, self.cfg, self.project, "s1", FACTS)
        for name, text in SOURCES.items():
            (tmp / name).write_text(text, encoding="utf-8")
        index_project(self.store, hashing, self.cfg, self.project, tmp)

    def recording(self, **kw) -> Recording:
        return Recording(self.cfg.dim, **kw)


class ReadPathsEmbedTheirQueryTests(_Fixture):
    def test_every_read_path_embeds_its_query_with_embed_query_only(self):
        reads = {
            "index block": lambda e: service.index_prompt_block(self.store, e, self.cfg, self.project, PROMPT),
            "search_code": lambda e: search_index(self.store, e, self.cfg, self.project, PROMPT, kind="code_symbol"),
            "review_memory": lambda e: service.review_memories(self.store, e, self.cfg, self.project, query=PROMPT),
            "recall tool": lambda e: service.recall_structured(self.store, e, self.cfg, self.project, PROMPT),
        }
        for name, read in reads.items():
            with self.subTest(read=name):
                embedder = self.recording()
                read(embedder)
                self.assertEqual(embedder.calls, Counter({("embed_query", PROMPT): 1}))

    def test_the_index_block_ranks_by_the_query_vector(self):
        # Asymmetric: the prompt's *query* vector is the keys symbol's text, so the index block must
        # surface rotate_keys first — embedding the prompt as a passage would surface ship_lambda.
        cfg = replace(self.cfg, index_top_k=1)
        embedder = self.recording(query_as={PROMPT: "Rotates the signing keys for the deploy pipeline."})
        block = service.index_prompt_block(self.store, embedder, cfg, self.project, PROMPT)
        self.assertIn("rotate_keys", block)
        passage = service.index_prompt_block(self.store, self.recording(), cfg, self.project, PROMPT)
        self.assertIn("ship_lambda", passage)  # the control: symmetric embedding ranks the other way


class OneEmbeddingPerPromptTests(_Fixture):
    def test_the_prompt_hook_embeds_the_prompt_once_for_both_blocks(self):
        embedder = self.recording()
        block = service.recall_prompt_block(self.store, embedder, self.cfg, self.project, PROMPT)
        self.assertIn(PROMPT_MEMORY_HEADER, block)  # both blocks ran, so both needed the vector
        self.assertIn(PROMPT_INDEX_HEADER, block)
        self.assertEqual(embedder.calls, Counter({("embed_query", PROMPT): 1}))

    def test_a_failing_embedder_leaves_the_prompt_hook_silent(self):
        class Broken(Recording):
            def embed_query(self, text):
                raise RuntimeError("model crashed")

        payload = io.StringIO(json.dumps({"prompt": PROMPT, "cwd": self.project["path"]}))
        out = io.StringIO()
        with (
            mock.patch("core.ports.embedding.get_embedder", lambda cfg: Broken(cfg.dim)),
            mock.patch("sys.stdin", payload),
            mock.patch("sys.stdout", out),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(recall_prompt.main(), 0)
        self.assertEqual(out.getvalue(), "")  # fail open: no output, so nothing is injected


class QueryMemoTests(unittest.TestCase):
    def setUp(self):
        self.inner = Recording(dim=32)
        self.memo = QueryMemo(self.inner)

    def test_it_reports_the_wrapped_gateways_shape(self):
        self.assertEqual((self.memo.dim, self.memo.semantic), (self.inner.dim, self.inner.semantic))

    def test_a_repeated_query_is_embedded_once(self):
        first, again = self.memo.embed_query("q"), self.memo.embed_query("q")
        self.assertEqual(first, again)
        self.assertEqual(self.inner.calls, Counter({("embed_query", "q"): 1}))

    def test_it_holds_one_entry_so_a_new_query_is_embedded_afresh(self):
        for text in ("a", "b", "a"):
            self.memo.embed_query(text)
        self.assertEqual(self.inner.calls, Counter({("embed_query", "a"): 2, ("embed_query", "b"): 1}))

    def test_stored_text_passes_straight_through(self):
        self.assertEqual(self.memo.embed(["x", "y"]), self.inner._hash.embed(["x", "y"]))
        self.assertEqual(self.memo.embed_one("x"), self.inner._hash.embed(["x"])[0])
        self.memo.embed_one("x")
        self.assertEqual(self.inner.methods(), Counter({"embed": 2, "embed_one": 2}))

    def test_each_wrapper_starts_empty(self):
        self.memo.embed_query("q")
        QueryMemo(self.inner).embed_query("q")  # a new call's memo: the vector is computed again
        self.assertEqual(self.inner.calls, Counter({("embed_query", "q"): 2}))


if __name__ == "__main__":
    unittest.main()
