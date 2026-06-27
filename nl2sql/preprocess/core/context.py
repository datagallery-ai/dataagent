from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

try:
    from ... import config
except ImportError:
    import config

from ...client import LlmClient
from .artifacts import ArtifactLayout


@dataclass
class Step1BuildContext:
    artifacts: ArtifactLayout
    force_rerun: bool = False
    verbose: bool = False
    db_filter: Optional[set[str]] = None
    limit: Optional[int] = None
    llm_client: Optional[LlmClient] = field(default=None, init=False)

    @classmethod
    def from_args(
        cls,
        *,
        force_rerun: bool = False,
        verbose: bool = False,
        db_ids: Optional[Iterable[str]] = None,
        limit: Optional[int] = None,
    ) -> "Step1BuildContext":
        db_filter = {str(item).strip() for item in (db_ids or []) if str(item).strip()} or None
        return cls(
            artifacts=ArtifactLayout.from_config(),
            force_rerun=force_rerun,
            verbose=verbose,
            db_filter=db_filter,
            limit=limit,
        )

    @property
    def bird_tables_json(self) -> Path:
        return Path(config.BIRD_TABLES_JSON)

    @property
    def bird_db_dir(self) -> Path:
        return Path(config.BIRD_DB_DIR)

    def should_process_db(self, db_id: str) -> bool:
        if not db_id:
            return False
        if self.db_filter is None:
            return True
        return db_id in self.db_filter

    def get_llm_client(self) -> LlmClient:
        if self.llm_client is None:
            api_base, model, api_key, extra_body = config.get_llm_config()
            self.llm_client = LlmClient(
                api_base=api_base,
                model=model,
                api_key=api_key,
                max_retries=config.LLM_MAX_RETRIES,
                retry_delay=config.LLM_RETRY_DELAY,
                backoff_multiplier=config.LLM_BACKOFF_MULTIPLIER,
                timeout=config.LLM_TIMEOUT,
                temperature=config.LLM_TEMPERATURE_GENERATION,
                max_tokens=config.LLM_MAX_TOKENS,
                verify_ssl=config.LLM_VERIFY_SSL,
                extra_body=extra_body,
            )
        return self.llm_client
