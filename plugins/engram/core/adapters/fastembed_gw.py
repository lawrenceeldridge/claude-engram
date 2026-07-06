"""Real local semantic embeddings via fastembed (ONNX) — optional.

Enable with ``embedding=fastembed`` in the plugin config after installing the
dep::

    pip install fastembed

The model loads once per process, which is why the resident daemon matters for
this adapter: a fresh model load on every UserPromptSubmit hook would add
seconds to each turn. With the daemon holding the model warm, queries stay fast.
"""

from __future__ import annotations

import math

from core.ports.embedding import EmbeddingGateway

# bge-base measured ~2.2x the Recall@1 of bge-small on the paraphrase benchmark
# for ~5ms/query (trivial with the warm daemon); int8 quantization loss is
# negligible, so no float rescore is needed. Override via embedding_model.
_DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"


def truncate_renorm(vec: list[float], dim: int) -> list[float]:
    """Matryoshka truncation: keep the first ``dim`` components, re-normalise to unit length.

    Only meaningful for Matryoshka-trained models (e.g. nomic-embed-text-v1.5),
    whose leading dimensions are trained to stand alone; on other models this
    simply loses information. A ``dim`` of 0 (or >= the native size) is a no-op.
    """
    if dim <= 0 or dim >= len(vec):
        return vec
    head = vec[:dim]
    norm = math.sqrt(sum(x * x for x in head))
    if norm > 0.0:
        head = [x / norm for x in head]
    return head


class FastEmbedGateway(EmbeddingGateway):
    def __init__(self, model_name: str | None = None, truncate_dim: int = 0) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name or _DEFAULT_MODEL)
        self._truncate = truncate_dim
        self.dim = len(self._cut(list(self._model.embed(["probe"]))[0].tolist()))

    def _cut(self, vec: list[float]) -> list[float]:
        return truncate_renorm(vec, self._truncate)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._cut(vec.tolist()) for vec in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # BGE is asymmetric: queries need the model's instruction prefix, which
        # fastembed applies via query_embed(). Fall back to plain embed if the
        # installed model/version lacks it.
        query_embed = getattr(self._model, "query_embed", None)
        if query_embed is None:
            return self.embed_one(text)
        return self._cut(list(query_embed([text]))[0].tolist())
