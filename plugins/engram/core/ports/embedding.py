"""Embedding gateway (port) + a dependency-free stub adapter.

The stub uses signed feature hashing: it maps shared vocabulary to nearby
vectors, so cosine similarity reflects *lexical* overlap. That is enough to prove
the capture -> store -> recall loop with zero installs. For genuine *semantic*
recall, set ``embedding=fastembed`` (a real local ONNX model) — the gateway
swaps without touching any call site (Ports & Adapters).
"""

from __future__ import annotations

import hashlib
import math
import re
import sys
from abc import ABC, abstractmethod

_TOKEN = re.compile(r"[a-z0-9]+")


class EmbeddingGateway(ABC):
    dim: int
    # Whether cosine over this gateway's vectors measures meaning (a real embedding model) rather
    # than token overlap. Recall confidence is calibrated only for semantic vectors.
    semantic: bool = True

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed_query(self, text: str) -> list[float]:
        """Embed a retrieval query — every read path embeds its query here; stored text (facts,
        chunks) goes through ``embed`` / ``embed_one``. Symmetric by default; a gateway whose model
        embeds queries differently from passages overrides it."""
        return self.embed_one(text)


class QueryMemo(EmbeddingGateway):
    """An ``EmbeddingGateway`` in front of another that embeds a query once, however many reads ask.

    The wrapped gateway's ``embed_query`` result for the last text is kept and served again;
    everything else passes straight through. ``recall_prompt_block`` wraps its embedder in one
    for the call, so the hook's two blocks — memory and index — share the prompt's vector instead
    of each paying for a model run (5–80 ms a prompt with bge-base). One entry, scoped to the call
    that made it — never a cross-turn cache (the daemon outlives every prompt).
    """

    def __init__(self, inner: EmbeddingGateway) -> None:
        self._inner = inner
        self.dim, self.semantic = inner.dim, inner.semantic
        self._last: tuple[str, list[float]] | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)

    def embed_one(self, text: str) -> list[float]:
        return self._inner.embed_one(text)

    def embed_query(self, text: str) -> list[float]:
        if self._last is None or self._last[0] != text:
            self._last = (text, self._inner.embed_query(text))
        return self._last[1]


class HashEmbedding(EmbeddingGateway):
    """Deterministic, dependency-free feature-hashing stub (lexical, not semantic)."""

    semantic = False

    def __init__(self, dim: int = 256, hashes: int = 2) -> None:
        self.dim = dim
        self.hashes = hashes

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            for h in range(self.hashes):
                digest = hashlib.blake2b(f"{h}:{tok}".encode(), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [x / norm for x in vec]
        return vec

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


def get_embedder(cfg) -> EmbeddingGateway:
    if cfg.embedding == "fastembed":
        try:
            from core.adapters.fastembed_gw import FastEmbedGateway

            return FastEmbedGateway(
                cfg.embedding_model or None,
                truncate_dim=getattr(cfg, "embedding_truncate_dim", 0),
            )
        except Exception as exc:  # fail-open to the stub — never break recall
            print(f"[engram] fastembed unavailable ({exc}); using hash stub", file=sys.stderr)
    return HashEmbedding(dim=cfg.dim)
