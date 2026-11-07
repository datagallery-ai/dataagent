"""Global configuration for DataLink."""

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """Configuration for LLM calls via OpenAI-compatible Chat Completions API.

    Three required parameters:
    - api_key: Authentication key
    - model: Model name (e.g., "gpt-4o", "deepseek-chat")
    - base_url: API endpoint URL (e.g., "https://api.openai.com/v1",
                "https://api.deepseek.com/v1", "http://localhost:11434/v1")

    Any service implementing the OpenAI Chat Completions protocol works.
    """

    model: str = Field(
        default="gpt-4o",
        description="Model name to use (e.g., gpt-4o, deepseek-chat, qwen-plus)",
    )
    api_key: str = Field(
        default="",
        description="API key (can also be set via OPENAI_API_KEY env var)",
    )
    base_url: str = Field(
        default="https://api.openai.com/v1",
        description="API endpoint base URL for OpenAI-compatible service",
    )
    temperature: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Temperature for LLM calls (low for deterministic mapping)",
    )
    max_tokens: int = Field(
        default=16384,
        description="Maximum tokens per LLM response",
    )
    timeout: float = Field(
        default=120.0,
        description="HTTP timeout in seconds for LLM API calls (includes retries). "
        "Increase this if your API gateway returns 504 before the SDK finishes. "
        "OpenAI SDK default is 600s, but self-hosted gateways often timeout sooner.",
    )

    def get_api_key(self) -> str:
        """Get API key from config or environment variable."""
        if self.api_key:
            return self.api_key
        return os.environ.get("OPENAI_API_KEY", "")


class EmbeddingConfig(BaseModel):
    """Configuration for embedding model calls via OpenAI-compatible API.

    Used by merge_with_existing for pre-filtering candidate matches.
    If model is empty, embedding pre-filtering is skipped and merge
    falls back to pure LLM judgment.
    """

    model: str = Field(
        default="",
        description="Embedding model name (e.g., text-embedding-3-small). Empty = skip embedding pre-filter.",
    )
    api_key: str = Field(
        default="",
        description="API key for embedding service. Falls back to LLMConfig.api_key if empty.",
    )
    base_url: str = Field(
        default="",
        description="API endpoint base URL for embedding service. Falls back to LLMConfig.base_url if empty.",
    )
    similarity_threshold: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="Minimum cosine similarity to consider as a merge candidate (pre-filter threshold).",
    )
    timeout: float = Field(
        default=60.0,
        description="HTTP timeout in seconds for embedding API calls. "
        "Embedding calls are typically faster than LLM calls, so the default is lower.",
    )

    def get_api_key(self, llm_config: LLMConfig | None = None) -> str:
        """Get API key from config or fall back to LLM config / env var."""
        if self.api_key:
            return self.api_key
        if llm_config and llm_config.api_key:
            return llm_config.api_key
        return os.environ.get("OPENAI_API_KEY", "")

    def get_base_url(self, llm_config: LLMConfig | None = None) -> str:
        """Get base URL from config or fall back to LLM config."""
        if self.base_url:
            return self.base_url
        if llm_config and llm_config.base_url:
            return llm_config.base_url
        return "https://api.openai.com/v1"

    def is_available(self, llm_config: LLMConfig | None = None) -> bool:
        """Check if embedding service is usable.

        Returns True when model is configured and API key is available.
        Used to decide whether to enable vector retrieval features.
        """
        if not self.model:
            return False
        return bool(self.get_api_key(llm_config))


class DataLinkConfig(BaseModel):
    """Top-level DataLink configuration."""

    llm: LLMConfig = Field(default_factory=LLMConfig, description="LLM configuration")
    embedding: EmbeddingConfig = Field(
        default_factory=EmbeddingConfig, description="Embedding model configuration for merge pre-filtering"
    )
    merge_llm_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Temperature for LLM merge judgment calls (low for deterministic matching)",
    )
    merge_batch_interval: int = Field(
        default=10,
        ge=1,
        description="Number of mapping batches to accumulate before running a merge. "
        "Higher values reduce merge LLM calls (1 = merge after every batch).",
    )
    mapping_batch_size: int = Field(
        default=15,
        ge=1,
        description="Number of columns per LLM mapping batch. "
        "Smaller values (e.g. 5) reduce prompt size and LLM response time, "
        "helpful when the API gateway has a short timeout (e.g. 60s). "
        "Default 15 works well for most OpenAI-compatible endpoints.",
    )
    graph_db_path: str = Field(
        default="datalink.db",
        description="SQLite database filename (resolved under ~/.datalink/storage/ unless an absolute path is given)",
    )
    sample_size: int = Field(
        default=1000,
        description="Default sample size for profiling",
    )
    confidence_threshold: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Minimum confidence threshold for inferred edges",
    )

    # Inferrer-specific thresholds
    joinable_overlap_threshold: float = Field(
        default=0.1,
        description="Minimum value overlap rate to consider columns joinable",
    )
    correlation_threshold: float = Field(
        default=0.5,
        description="Minimum absolute correlation coefficient to create an edge",
    )

    # MCP auxiliary tools — comma-separated full tool names to expose alongside
    # the default datalink_explore + write tools. Empty string (default) means
    # only core tools are visible to agents.
    # Example: "datalink_search_nodes,datalink_get_node,datalink_find_paths"
    # Can also be set via DATALINK_MCP_TOOLS env var (env var takes precedence).
    mcp_tools: str = Field(
        default="",
        description="Comma-separated auxiliary MCP tool names to expose (empty = only core tools)",
    )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "DataLinkConfig":
        """Load configuration from a JSON file.

        If path is None, looks for datalink_config.json in current directory,
        then falls back to defaults.
        """
        if path is None:
            path = Path.cwd() / "datalink_config.json"
        else:
            path = Path(path)

        if path.exists():
            with open(path) as f:
                data = json.load(f)
            return cls(**data)
        return cls()

    def save(self, path: str | Path | None = None) -> None:
        """Save configuration to a JSON file."""
        if path is None:
            path = Path.cwd() / "datalink_config.json"
        else:
            path = Path(path)

        with open(path, "w") as f:
            json.dump(self.model_dump(), f, indent=2)
