"""Real local semantic embeddings via fastembed (ONNX) — optional.

Enable with ``embedding=fastembed`` in the plugin config after installing the
dep::

    pip install fastembed

The model loads once per process, which is why the resident daemon matters for
this adapter: a fresh model load on every UserPromptSubmit hook would add
seconds to each turn. With the daemon holding the model warm, queries stay fast.
"""

from __future__ import annotations

from core.ports.embedding import EmbeddingGateway

# bge-base measured ~2.2x the Recall@1 of bge-small on the paraphrase benchmark
# for ~5ms/query (trivial with the warm daemon); int8 quantization loss is
# negligible, so no float rescore is needed. Override via embedding_model.
_DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"


class FastEmbedGateway(EmbeddingGateway):
    def __init__(self, model_name: str | None = None) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name or _DEFAULT_MODEL)
        self.dim = len(list(self._model.embed(["probe"]))[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # BGE is asymmetric: queries need the model's instruction prefix, which
        # fastembed applies via query_embed(). Fall back to plain embed if the
        # installed model/version lacks it.
        query_embed = getattr(self._model, "query_embed", None)
        if query_embed is None:
            return self.embed_one(text)
        return list(query_embed([text]))[0].tolist()
