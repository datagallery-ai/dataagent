# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""dataagent — 基于 openjiuwen DeepAgent 的数据分析框架。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"
__author__ = "DataAgent Team"

__all__ = [
    "DataAgent",
    "load_agent_from_config",
]

if TYPE_CHECKING:
    from dataagent.interface.sdk import DataAgent, load_agent_from_config


def __getattr__(name: str) -> Any:
    """Lazily expose the public SDK without importing OpenJiuWen at package import time."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from dataagent.interface.sdk import DataAgent, load_agent_from_config

    public_api = {
        "DataAgent": DataAgent,
        "load_agent_from_config": load_agent_from_config,
    }
    value = public_api[name]
    globals()[name] = value
    return value


def _silence_jiuwen_logging() -> None:
    """Suppress jiuwen console INFO logs — only show WARNING and above.

    Must be called **after** jiuwen is first imported, because jiuwen
    initialises its logging on first import.  ``DataAgent.from_config``
    calls this automatically.
    """
    try:
        import copy

        from openjiuwen.core.common.logging.log_config import configure_log_config, log_config

        snap = log_config.get_snapshot()
        if isinstance(snap, dict):
            new_cfg = copy.deepcopy(snap)
            new_cfg["level"] = "WARNING"
            configure_log_config(new_cfg)
    except Exception:
        pass
