"""Semantic-service client, independent of plugin registration and Agent execution."""

from dataagent.semantic_client.client import SemanticServiceClient, normalize_semantic_base_url

__all__ = ["SemanticServiceClient", "normalize_semantic_base_url"]
