"""Text embeddings for retrieval (feasibility-and-cost.md: Tier-1, local, $0).

A small DI port so the model stays swappable — it is a measure-then-fix parameter (text-inference.md
§10) — and so tests inject a fake without downloading weights. The real impl is `fastembed`
(ONNX, no PyTorch) running `bge-small-en-v1.5` (384-dim); the model downloads once on first use.
"""

from functools import lru_cache
from typing import Protocol

from fastembed import TextEmbedding

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384


class Embedder(Protocol):
    """What ingestion/retrieval need: a fixed output dimension + a batch embed."""

    @property
    def dimension(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    """`fastembed` `bge-small-en-v1.5` — local CPU embeddings, no PyTorch."""

    def __init__(self, model_name: str = EMBEDDING_MODEL) -> None:
        self._model = TextEmbedding(model_name=model_name)

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch; each vector is `EMBEDDING_DIM` floats (cosine space)."""
        return [[float(value) for value in vector] for vector in self._model.embed(texts)]


@lru_cache(maxsize=1)
def default_embedder() -> FastEmbedEmbedder:
    """The process-wide embedder — loads the model once (used by the worker/ingestion at M1.4+)."""
    return FastEmbedEmbedder()
