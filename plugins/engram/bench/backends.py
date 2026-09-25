"""Embedding-backend specs for the bench: ``name[@model][%truncate_dim][+float]``."""

from __future__ import annotations

from core.ports.embedding import EmbeddingGateway, HashEmbedding


def parse_spec(spec: str) -> tuple[str, str | None, int, bool]:
    """``name[@model][%truncate_dim][+float]`` — %N truncates Matryoshka vectors to N dims."""
    float_mode = spec.endswith("+float")
    core = spec[: -len("+float")] if float_mode else spec
    core, _, trunc = core.partition("%")
    name, _, model = core.partition("@")
    return name, (model or None), int(trunc) if trunc else 0, float_mode


def make_embedder(name: str, model: str | None, truncate_dim: int, cfg) -> EmbeddingGateway:
    if name == "hash":
        return HashEmbedding(dim=cfg.dim)
    if name == "fastembed":
        from core.adapters.fastembed_gw import FastEmbedGateway

        return FastEmbedGateway(model, truncate_dim=truncate_dim)
    raise ValueError(f"unknown backend {name!r}")
