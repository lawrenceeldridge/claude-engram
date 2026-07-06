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

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed_query(self, text: str) -> list[float]:
        """Embed a retrieval query. Symmetric by default; asymmetric models
        (e.g. BGE) override this to apply their query instruction prefix."""
        return self.embed_one(text)


class HashEmbedding(EmbeddingGateway):
    """Deterministic, dependency-free feature-hashing stub (lexical, not semantic)."""

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
