from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SYSTEM_PROMPT = (
    "You are DataFoundry's assistant. "
    "Data warehouse tools, knowledge retrieval, and skill packages are not connected in this runtime version. "
    "You may converse, use built-in planning/todo/filesystem tools, and ask the user questions when you need confirmation."
)


@dataclass(frozen=True)
class RuntimeSettings:
    host: str = "127.0.0.1"
    port: int = 8790
    token: str | None = None
    llm_model: str = "qwen-plus"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    fake_model: bool = False

    @property
    def model_configured(self) -> bool:
        return self.fake_model or bool(self.llm_api_key)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RuntimeSettings:
        source = env if env is not None else os.environ
        token = (source.get("RUNTIME_SERVICE_TOKEN") or "").strip() or None
        model_mode = (source.get("DEEPAGENTS_RUNTIME_MODEL") or "").strip().lower()
        api_key = (source.get("LLM_API_KEY") or source.get("OPENAI_API_KEY") or "").strip() or None
        if model_mode == "fake":
            fake_model = True
        elif model_mode == "live":
            fake_model = False
        else:
            fake_model = api_key is None
        return cls(
            host=(source.get("RUNTIME_HOST") or source.get("RUNTIME_SERVICE_HOST") or "127.0.0.1").strip(),
            port=_port(source.get("RUNTIME_PORT") or source.get("RUNTIME_SERVICE_PORT") or "8790"),
            token=token,
            llm_model=(source.get("LLM_MODEL") or "qwen-plus").strip(),
            llm_base_url=(source.get("LLM_BASE_URL") or "").strip() or None,
            llm_api_key=api_key,
            fake_model=fake_model,
        )


def _port(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 65535:
        raise ValueError("RUNTIME_PORT must be between 1 and 65535")
    return parsed
