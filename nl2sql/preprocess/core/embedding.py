from __future__ import annotations

import logging
from typing import Iterable, List

import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class EmbeddingEncoder:
    def __init__(self, model_name: str, device: str = "cpu"):
        self._model_name = model_name
        self._device = device
        self._model = SentenceTransformer(model_name, device=device, trust_remote_code=True)
        logger.info("EmbeddingEncoder initialized: model=%s device=%s", model_name, device)

    def encode(self, texts: Iterable[str]) -> np.ndarray:
        text_list: List[str] = [str(item or "") for item in texts]
        if not text_list:
            return np.empty((0, 1), dtype=np.float32)
        vectors = self._model.encode(text_list, normalize_embeddings=False)
        return np.array(vectors, dtype=np.float32)
