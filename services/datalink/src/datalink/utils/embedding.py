"""Embedding service for DataLink — compute and manage text embedding vectors.

Provides a reusable EmbeddingService that wraps OpenAI-compatible embedding API calls.
Used by:
- GraphRetrieval: hybrid search (text + vector)
- BuildPipeline: pre-build embedding vectors during rebuild/add_table
- LLMMapper: embedding pre-filter for merge_with_existing (existing usage)
"""

import logging

from datalink.config import EmbeddingConfig, LLMConfig
from datalink.models.node import ColumnNode, ConceptNode, EntityNode, Node, TableNode
from datalink.models.profile import ColumnProfile

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Embedding computation service using OpenAI-compatible API.

    If the embedding model is not configured (model field is empty),
    all operations gracefully return empty results — vector retrieval
    is skipped and full-text search works as before.
    """

    def __init__(self, embedding_config: EmbeddingConfig, llm_config: LLMConfig | None = None):
        """Initialize with embedding and LLM configuration.

        Args:
            embedding_config: Embedding model configuration.
            llm_config: LLM configuration (for API key/base_url fallback).
        """
        self.embedding_config = embedding_config
        self.llm_config = llm_config or LLMConfig()

    def is_available(self) -> bool:
        """Check if embedding service is usable.

        Returns True when embedding model is configured and API key is available.
        """
        if not self.embedding_config.model:
            return False
        api_key = self.embedding_config.get_api_key(self.llm_config)
        return bool(api_key)

    def compute_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Compute embedding vectors for a batch of texts.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors (list[float] each).
            Returns empty list if service is not available or call fails.
        """
        if not self.is_available() or not texts:
            return []

        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=self.embedding_config.get_api_key(self.llm_config),
                base_url=self.embedding_config.get_base_url(self.llm_config),
                timeout=self.embedding_config.timeout,
            )

            response = client.embeddings.create(
                model=self.embedding_config.model,
                input=texts,
            )

            vectors = [item.embedding for item in response.data]
            logger.info(f"Computed {len(vectors)} embeddings via {self.embedding_config.model}")
            return vectors

        except Exception as e:
            logger.warning(f"Embedding computation failed: {e}")
            return []

    def compute_embedding(self, text: str) -> list[float]:
        """Compute embedding vector for a single text.

        Args:
            text: Text string to embed.

        Returns:
            Embedding vector (list[float]), or empty list if unavailable.
        """
        result = self.compute_embeddings([text])
        return result[0] if result else []

    def get_model_name(self) -> str:
        """Get the configured embedding model name."""
        return self.embedding_config.model


def node_to_searchable_text(
    node: Node,
    profile: ColumnProfile | None = None,
    column_names: list[str] | None = None,
) -> str:
    """Convert a node to searchable text for embedding generation.

    This produces a rich text representation that captures the node's
    semantic meaning — combining name, description, type info, and
    representative values. Used both for embedding generation and
    for debugging/validation in the node_embeddings table.

    Args:
        node: The node to convert.
        profile: Optional ColumnProfile (for Column nodes).
        column_names: Optional list of column names (for Table nodes).

    Returns:
        Pipe-delimited text string suitable for embedding.
    """
    parts: list[str] = [node.name]

    if isinstance(node, ColumnNode):
        if node.comment:
            parts.append(node.comment)
        if node.semantic_type and node.semantic_type != "unknown":
            parts.append(node.semantic_type)
        if node.dtype:
            parts.append(f"type: {node.dtype}")
        if profile:
            if profile.top_values:
                vals = [str(tv["value"]) for tv in profile.top_values[:5]]
                parts.append("values: " + ", ".join(vals))
            elif profile.sample_values:
                vals = [str(v) for v in profile.sample_values[:5]]
                parts.append("samples: " + ", ".join(vals))

    elif isinstance(node, ConceptNode):
        if node.description:
            parts.append(node.description)
        if node.unit:
            parts.append(f"unit: {node.unit}")
        if node.dimension:
            parts.append(f"dimension: {node.dimension}")

    elif isinstance(node, EntityNode):
        if node.description:
            parts.append(node.description)

    elif isinstance(node, TableNode):
        comment = node.properties.get("comment", "")
        if comment:
            parts.append(comment)
        if column_names:
            parts.append("columns: " + ", ".join(column_names[:10]))

    return " | ".join(parts)
