"""Bench backend specs (``bench/backends.py``): the spec grammar and the one place backends are built."""

from __future__ import annotations

import unittest
from dataclasses import replace

import _harness  # noqa: F401

from bench.backends import Spec, make_embedder, parse_spec, store_embedder
from core.config import get_config
from core.ports.embedding import HashEmbedding


class ParseSpecTests(unittest.TestCase):
    def test_every_part_of_the_grammar(self):
        cases = {
            "hash": Spec("hash", None, 0, False),
            "fastembed": Spec("fastembed", None, 0, False),
            "fastembed@BAAI/bge-base-en-v1.5": Spec("fastembed", "BAAI/bge-base-en-v1.5", 0, False),
            "fastembed@nomic-ai/nomic-embed-text-v1.5%256": Spec(
                "fastembed", "nomic-ai/nomic-embed-text-v1.5", 256, False
            ),
            "fastembed+float": Spec("fastembed", None, 0, True),
            "fastembed@BAAI/bge-small-en-v1.5+float": Spec("fastembed", "BAAI/bge-small-en-v1.5", 0, True),
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(parse_spec(spec), expected)

    def test_an_unknown_flag_is_an_error_not_part_of_the_model(self):
        for spec in ("fastembed+flaot", "fastembed@BAAI/bge-base-en-v1.5+int4"):
            with self.subTest(spec=spec), self.assertRaisesRegex(ValueError, "unknown backend flag"):
                parse_spec(spec)


class MakeEmbedderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(get_config(), distiller="heuristic")

    def test_hash_builds_the_lexical_stub(self):
        self.assertIsInstance(make_embedder(parse_spec("hash"), self.cfg), HashEmbedding)

    def test_an_unknown_backend_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown backend"):
            make_embedder(parse_spec("word2vec"), self.cfg)

    def test_a_store_ranking_harness_refuses_an_in_memory_spec(self):
        with self.assertRaisesRegex(ValueError, "LongMemEval needs the real ranking paths"):
            store_embedder(parse_spec("hash+float"), self.cfg, "LongMemEval")
        self.assertIsInstance(store_embedder(parse_spec("hash"), self.cfg, "LongMemEval"), HashEmbedding)


if __name__ == "__main__":
    unittest.main()
