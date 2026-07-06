"""Embeddings for semantic recall.

An ``Embedder`` Protocol decouples Memory from any specific model, and a
``LocalEmbedder`` default runs fully offline (fastembed / all-MiniLM-L6-v2,
384-dim) so recall never touches Bifrost or the network at query time
(the model is fetched once on first use). Matches the spec's embedding choice.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from fastembed import TextEmbedding

_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_DEFAULT_DIM = 384


@runtime_checkable
class Embedder(Protocol):
    """Turns text into a fixed-width float vector."""

    @property
    def dim(self) -> int:
        """Dimensionality of the produced vectors."""
        ...

    def embed(self, text: str) -> list[float]:
        """Embed one string."""
        ...


class LocalEmbedder:
    """Offline embedder backed by fastembed (ONNX, no torch, no cloud)."""

    def __init__(self, model_name: str = _DEFAULT_MODEL, dim: int = _DEFAULT_DIM) -> None:
        self._model = TextEmbedding(model_name=model_name)
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, text: str) -> list[float]:
        vector = next(iter(self._model.embed([text])))
        return [float(x) for x in vector]
