"""Compile dedicated and general YAML subagents into native Deep Agents specs."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.subagents import CompiledSubAgent
from langchain_core.language_models import BaseChatModel
from langgraph.store.base import BaseStore

from dataagent.agents.nl2sql.agent import create_nl2sql_agent
from dataagent.config import ConfigManager
from dataagent.core.deepagents.config.models import ModelConfigCompiler

_DEFAULT_NL2SQL_METADATA = {
    "id": "nl2sql",
    "name": "NL2SQL Agent",
    "description": "Generate, validate, and execute read-only SQL, then save the SQL and CSV result.",
    "type": "nl2sql",
}
_NATIVE_AGENT_TYPES = frozenset({"deepagent", "react"})


class SubagentConfigCompiler:
    """Compile ``NL2SQL`` and canonical ``SUBAGENTS`` configuration sections."""

    def __init__(
        self,
        config: Mapping[str, Any],
        models: Mapping[str, BaseChatModel],
        primary_model_name: str,
        backend: BackendProtocol,
        *,
        store: BaseStore | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        visited_paths: frozenset[Path] = frozenset(),
    ) -> None:
        self._config = config
        self._models = models
        self._primary_model_name = primary_model_name
        self._backend = backend
        self._store = store
        self._user_id = user_id
        self._session_id = session_id
        self._visited_paths = visited_paths

    async def compile(self) -> tuple[CompiledSubAgent, ...]:
        """Compile all configured subagents and reject duplicate identifiers."""
        compiled: list[CompiledSubAgent] = []
        inline_nl2sql = self._config.get("NL2SQL")
        if inline_nl2sql is not None:
            if not isinstance(inline_nl2sql, Mapping):
                raise ValueError("NL2SQL must be a mapping.")
            compiled.append(self._compile_nl2sql(inline_nl2sql, dedicated=True))

        raw_subagents = self._config.get("SUBAGENTS", ())
        if raw_subagents is None:
            raw_subagents = ()
        if isinstance(raw_subagents, (str, bytes)) or not isinstance(raw_subagents, Sequence):
            raise ValueError("SUBAGENTS must be a list of mappings with absolute YAML paths.")
        for index, entry in enumerate(raw_subagents):
            compiled.append(await self._compile_path_entry(entry, index))

        names = [str(spec.get("name", "")) for spec in compiled]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Subagent identifiers must be unique; duplicates: {', '.join(duplicates)}.")
        return tuple(compiled)

    async def _compile_path_entry(self, entry: Any, index: int) -> CompiledSubAgent:
        if not isinstance(entry, Mapping):
            raise ValueError(f"SUBAGENTS[{index}] must be a mapping.")
        path = self._resolve_path(entry.get("path"), index)
        if path in self._visited_paths:
            raise ValueError(f"Recursive SUBAGENTS reference detected: {path}")

        manager = ConfigManager()
        manager.reload(str(path), default_config_path=None)
        child_config = manager.get_all()
        agent_config = self._agent_metadata(child_config, source=str(path))
        agent_type = str(agent_config.get("type", "react") or "react").strip().lower()
        if agent_type == "nl2sql":
            return self._compile_nl2sql(child_config, dedicated=False)
        if agent_type not in _NATIVE_AGENT_TYPES:
            raise ValueError(f"Unsupported SUBAGENTS agent type {agent_type!r} in {path}.")

        from dataagent.core.deepagents.agent import create_data_agent
        from dataagent.core.deepagents.config.compiler import DeepAgentConfigCompiler

        has_models = bool(_mapping(child_config.get("MODEL")))
        child_models = None if has_models else self._models
        child_primary = None if has_models else self._primary_model_name
        child_compiled = await DeepAgentConfigCompiler(
            child_config,
            config_manager=manager,
            models=child_models,
            primary_model_name=child_primary,
            backend=self._backend,
            checkpointer=False,
            store=self._store,
            user_id=self._user_id,
            session_id=self._session_id,
            subagent_paths=self._visited_paths | {path},
        ).compile()
        runnable = await create_data_agent(child_compiled)
        return {
            "name": self._identifier(agent_config),
            "description": str(agent_config.get("description", "") or "").strip(),
            "runnable": runnable,
        }

    def _compile_nl2sql(self, config: Mapping[str, Any], *, dedicated: bool) -> CompiledSubAgent:
        effective = copy.deepcopy(dict(config))
        raw_metadata = _mapping(effective.get("AGENT_CONFIG"))
        metadata = dict(_DEFAULT_NL2SQL_METADATA if dedicated else {})
        metadata.update(raw_metadata)
        metadata.setdefault("type", "nl2sql")
        if str(metadata.get("type", "") or "").strip().lower() != "nl2sql":
            location = "NL2SQL.AGENT_CONFIG.type" if dedicated else "AGENT_CONFIG.type"
            raise ValueError(f"{location} must be 'nl2sql'.")
        effective.update({"AGENT_CONFIG": metadata})
        for section in ("DATABASE", "SEMANTIC_LAYER"):
            parent_value = self._config.get(section)
            if isinstance(parent_value, Mapping):
                effective.update({section: copy.deepcopy(dict(parent_value))})

        model = self._resolve_nl2sql_model(effective, metadata)
        identifier = self._identifier(metadata)
        description = str(metadata.get("description", "") or "").strip()
        if not description:
            raise ValueError(f"NL2SQL subagent '{identifier}' requires AGENT_CONFIG.description.")
        runnable = create_nl2sql_agent(effective, model, self._backend, name=identifier)
        return {"name": identifier, "description": description, "runnable": runnable}

    def _resolve_nl2sql_model(
        self,
        config: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> BaseChatModel:
        if _mapping(config.get("MODEL")):
            compiler = ModelConfigCompiler(config)
            models = compiler.compile()
            requested = str(metadata.get("primary_model", "") or "").strip() or None
            primary_name = compiler.resolve_primary_model_name(models, requested)
            model = models.get(primary_name)
        else:
            model = self._models.get(self._primary_model_name)
        if model is None:
            raise ValueError("NL2SQL primary model is not available.")
        return model

    @staticmethod
    def _resolve_path(raw_path: Any, index: int) -> Path:
        value = str(raw_path or "").strip()
        if not value:
            raise ValueError(f"SUBAGENTS[{index}].path is required.")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError(f"SUBAGENTS[{index}].path must be absolute or start with ~/.")
        resolved = path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"SUBAGENTS[{index}].path does not exist: {resolved}")
        return resolved

    @staticmethod
    def _agent_metadata(config: Mapping[str, Any], *, source: str) -> Mapping[str, Any]:
        metadata = config.get("AGENT_CONFIG")
        if not isinstance(metadata, Mapping):
            raise ValueError(f"{source} must contain an AGENT_CONFIG mapping.")
        if not str(metadata.get("name", "") or "").strip():
            raise ValueError(f"{source} is missing AGENT_CONFIG.name.")
        if not str(metadata.get("description", "") or "").strip():
            raise ValueError(f"{source} is missing AGENT_CONFIG.description.")
        return metadata

    @staticmethod
    def _identifier(metadata: Mapping[str, Any]) -> str:
        raw = str(metadata.get("id") or metadata.get("name") or "").strip()
        identifier = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-").lower()
        if not identifier:
            raise ValueError("Subagent AGENT_CONFIG.id or name must contain an identifier.")
        return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = ["SubagentConfigCompiler"]
