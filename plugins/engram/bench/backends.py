"""Embedding-backend specs for the bench: ``name[@model][%dim][+float]``."""

from __future__ import annotations

from typing import NamedTuple

from core.ports.embedding import EmbeddingGateway, HashEmbedding

FLAGS = frozenset({"float"})


class Spec(NamedTuple):
    """A parsed backend spec — what ``make_embedder`` builds and how a harness may rank with it."""

    name: str
    model: str | None
    truncate_dim: int
    float_mode: bool  # ``+float``: rank raw full-precision vectors in memory, not through the store


def parse_spec(spec: str) -> Spec:
    """``name[@model][%dim][+float]`` — ``%dim`` truncates Matryoshka vectors to ``dim`` dims;
    ``+float`` is described on :class:`Spec`. An unknown flag is an error, not part of the model."""
    core, *flags = spec.split("+")
    unknown = set(flags) - FLAGS
    if unknown:
        raise ValueError(f"unknown backend flag(s) {sorted(unknown)} in {spec!r}; expected {sorted(FLAGS)}")
    core, _, trunc = core.partition("%")
    name, _, model = core.partition("@")
    return Spec(name, model or None, int(trunc) if trunc else 0, "float" in flags)


def make_embedder(spec: Spec, cfg) -> EmbeddingGateway:
    if spec.name == "hash":
        return HashEmbedding(dim=cfg.dim)
    if spec.name == "fastembed":
        from core.adapters.fastembed_gw import FastEmbedGateway

        return FastEmbedGateway(spec.model, truncate_dim=spec.truncate_dim)
    raise ValueError(f"unknown backend {spec.name!r}")


def store_embedder(spec: Spec, cfg, harness: str) -> EmbeddingGateway:
    """The embedder for a harness that ranks through the real store, which ``+float`` (an
    in-memory ranking) cannot stand in for."""
    if spec.float_mode:
        raise ValueError(f"+float ranks outside the store; {harness} needs the real ranking paths")
    return make_embedder(spec, cfg)
