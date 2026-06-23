# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""OpenJiuWen checkpointer configuration and process-global lifecycle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SUPPORTED_CHECKPOINTER_TYPES = {"in_memory", "persistence", "redis"}


@dataclass(frozen=True)
class CheckpointerSpec:
    """Normalized OpenJiuWen checkpointer configuration."""

    type: str = "in_memory"
    conf: Mapping[str, Any] | None = None

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        workspace_root: str | Path,
    ) -> CheckpointerSpec:
        raw = _config_value(config, "CHECKPOINTER")
        if raw is None:
            raw = _config_value(config, "CHECKPOINT")
        if raw is None:
            return cls(conf={})
        if not isinstance(raw, Mapping):
            raise ValueError("CHECKPOINTER must be a mapping")

        checkpointer_type = str(raw.get("type", "in_memory")).strip().lower()
        if checkpointer_type not in _SUPPORTED_CHECKPOINTER_TYPES:
            supported = ", ".join(sorted(_SUPPORTED_CHECKPOINTER_TYPES))
            raise ValueError(
                f"CHECKPOINTER.type must be one of {supported}; got {checkpointer_type!r}"
            )

        raw_conf = raw.get("conf", {})
        if raw_conf is None:
            raw_conf = {}
        if not isinstance(raw_conf, Mapping):
            raise ValueError("CHECKPOINTER.conf must be a mapping")
        conf = dict(raw_conf)

        if checkpointer_type == "persistence":
            db_type = str(conf.get("db_type", "sqlite")).strip().lower()
            if db_type not in {"sqlite", "shelve"}:
                raise ValueError(
                    "CHECKPOINTER.conf.db_type must be 'sqlite' or 'shelve'"
                )
            conf["db_type"] = db_type
            db_path = conf.get("db_path")
            if db_path is None or not str(db_path).strip():
                db_path = Path(workspace_root) / ".checkpoints" / "dataagent"
            else:
                db_path = Path(str(db_path)).expanduser()
                if not db_path.is_absolute():
                    db_path = Path(workspace_root) / db_path
            conf["db_path"] = str(db_path.resolve())

        if checkpointer_type == "redis":
            connection = conf.get("connection")
            if not isinstance(connection, Mapping):
                raise ValueError(
                    "CHECKPOINTER.conf.connection must be a mapping for redis"
                )
            if not connection.get("url") and not connection.get("redis_client"):
                raise ValueError(
                    "CHECKPOINTER.conf.connection requires url or redis_client"
                )

        return cls(type=checkpointer_type, conf=conf)

    def fingerprint(self) -> str:
        return json.dumps(
            {"type": self.type, "conf": dict(self.conf or {})},
            sort_keys=True,
            default=repr,
            separators=(",", ":"),
        )


class _CheckpointerRuntime:
    """Serialize changes to OpenJiuWen's process-global default checkpointer."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._fingerprint: str | None = None
        self._active_leases = 0

    @asynccontextmanager
    async def lease(self, spec: CheckpointerSpec) -> AsyncIterator[None]:
        fingerprint = spec.fingerprint()
        async with self._lock:
            if (
                self._active_leases
                and self._fingerprint is not None
                and self._fingerprint != fingerprint
            ):
                raise RuntimeError(
                    "OpenJiuWen CheckpointerFactory is process-global; "
                    "different CHECKPOINTER configurations cannot run concurrently"
                )
            if self._fingerprint != fingerprint:
                await _install_default_checkpointer(spec)
                self._fingerprint = fingerprint
            self._active_leases += 1

        try:
            yield
        finally:
            async with self._lock:
                self._active_leases -= 1


_runtime = _CheckpointerRuntime()


def build_checkpointer_spec(
    config: Any,
    *,
    workspace_root: str | Path,
) -> CheckpointerSpec:
    """Build the normalized checkpointer spec from DataAgent YAML."""
    return CheckpointerSpec.from_config(config, workspace_root=workspace_root)


def checkpointer_lease(spec: CheckpointerSpec):
    """Hold a stable process-global checkpointer for one agent execution."""
    return _runtime.lease(spec)


async def _install_default_checkpointer(spec: CheckpointerSpec) -> None:
    from openjiuwen.core.session.checkpointer.checkpointer import (
        CheckpointerConfig,
        CheckpointerFactory,
    )

    if spec.type == "redis":
        import openjiuwen.extensions.checkpointer.redis.checkpointer  # noqa: F401

    checkpointer = await CheckpointerFactory.create(
        CheckpointerConfig(type=spec.type, conf=dict(spec.conf or {}))
    )
    CheckpointerFactory.set_default_checkpointer(checkpointer)


def _config_value(config: Any, key: str) -> Any:
    if isinstance(config, Mapping):
        return config.get(key)
    if hasattr(config, "get"):
        return config.get(key)
    raise TypeError(
        f"config must be a mapping or provide get(), got {type(config).__name__}"
    )
