# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
import os
from typing import Any

import httpx
import numpy as np

from dataagent.core.managers.llm_manager import LLMConfig, llm_manager

EMBEDDING_DIMENSIONS = 1024


def embedding(
    query: str | list[str],
    *,
    embedding_model: str,
) -> np.ndarray:
    """Embed queries using an OpenAI-compatible ``/embeddings`` endpoint via httpx.

    设计与 :mod:`dataagent.core.managers.llm_manager.llm_client` 对齐——零三方 SDK，
    只用 ``httpx`` 直连 OpenAI 兼容协议，不再引入 litellm。

    Args:
        query: Text or list of texts to embed.
        embedding_model: Model key from per-Agent ``MEMORY.embedding_model`` (required).
    """
    model_key = embedding_model
    config: LLMConfig | None = llm_manager.get_llm_config(model_key)
    if config is None:
        raise RuntimeError("Embedding model not found.")
    kwargs = dict(config.client_kwargs).get("params", {})
    model = kwargs.get("model", "")
    base_url = os.getenv("EMBEDDING_BASE_URL") or kwargs.get("base_url", "")
    api_key = os.getenv("EMBEDDING_API_KEY") or kwargs.get("api_key", "")
    if not base_url:
        raise RuntimeError("EMBEDDING_BASE_URL is not set.")
    if not api_key:
        raise RuntimeError("EMBEDDING_API_KEY is not set.")

    endpoint = f"{str(base_url).rstrip('/')}/embeddings"
    payload: dict[str, Any] = {
        "model": model,
        "input": query,
        "dimensions": EMBEDDING_DIMENSIONS,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=60.0, verify=False) as client:
        resp = client.post(endpoint, headers=headers, json=payload)
        resp.raise_for_status()
        body = resp.json()

    data = body["data"]
    output = np.array([item["embedding"] for item in data])
    if isinstance(query, str):
        output = output[0]
    return output


def model_inference(query: str) -> str:
    """Inference using LLM."""
    llm = llm_manager.get_default_llm()
    output = llm.invoke([{"role": "user", "content": query}]).content
    return output


def cosine_similarity(X: np.ndarray, Y: np.ndarray | None = None) -> np.ndarray:
    """Compute cosine similarity between vectors/matrices. Equivalent to sklearn.metrics.pairwise.cosine_similarity."""
    X = np.asarray(X, dtype=np.float64)
    X_norm = np.linalg.norm(X, axis=1, keepdims=True)
    X_norm = np.where(X_norm == 0, 1e-8, X_norm)
    X_normalized = X / X_norm
    if Y is None:
        return X_normalized @ X_normalized.T
    Y = np.asarray(Y, dtype=np.float64)
    Y_norm = np.linalg.norm(Y, axis=1, keepdims=True)
    Y_norm = np.where(Y_norm == 0, 1e-8, Y_norm)
    Y_normalized = Y / Y_norm
    return X_normalized @ Y_normalized.T
