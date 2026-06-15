# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""从 Flex YAML ``config`` 构建 :class:`~dataagent.core.cbb.agent_env.Env` 与 :class:`~dataagent.core.cbb.runtime.Runtime`。

职责：把 ``MODEL`` 段与各节点 ``chat_model`` 合并，解析 ``api_base`` / ``api_key``。
优先取自 ``params.base_url`` / ``params.api_key``；未配置时再读环境变量
``{PROVIDER}_BASE_URL`` / ``{PROVIDER}_API_KEY``，写入 ``env.llm_configs``。

``env.llm_configs`` 的 **键** 为节点名、``HOOKS`` 里声明的 hook ``name``（见
:func:`_merge_hook_llm_configs`），或 ``MODEL`` 中未挂节点的模型名；**值** 只存调用所需扁平参数：
``model`` / ``api_base`` / ``api_key``，以及原 ``params`` 展开后的 litellm 透传项
（如 ``temperature``）。不在 env 中存放 ``name`` / ``provider`` / ``section`` / ``model_type``（这些仅在
解析 YAML 时使用）。

与 :mod:`dataagent.core.managers.llm_manager.llm_client` 的分界：本模块只做「YAML + env → ``env.llm_configs``」；
运行时用 :func:`~dataagent.core.managers.llm_manager.llm_client.llm_adapter_from_env_cfg` 由 env 项构造适配器（``runtime.llm`` 懒加载）。
"""

from __future__ import annotations

import os
from typing import Any

from dataagent.core.cbb.agent_env import Env as AgentEnv
from dataagent.core.cbb.runtime import Runtime

# YAML 合并阶段使用、不写入 env.llm_configs 值的键
_LLM_YAML_ONLY_KEYS = frozenset({"name", "provider", "model_type", "section", "params"})


def resolve_llm_config_entry(
    *,
    model_section: dict[str, Any],
    entry: dict[str, Any] | str,
    model_key_override: str | None = None,
) -> dict[str, Any]:
    """Merge YAML ``MODEL[model_key]`` with node entry and resolve ``api_base`` / ``api_key``.

    返回 **仅含调用参数** 的扁平 dict（无 ``name``/``provider``/``section``）；``provider`` 仅用于读 env。
    """
    if isinstance(entry, str):
        entry = {"name": entry}
    model_key = model_key_override or str(entry.get("name") or "").strip()
    if not model_key:
        raise ValueError("LLM config entry must include 'name'")
    base = dict(model_section.get(model_key) or {})
    merged: dict[str, Any] = {**base, **entry}
    merged.setdefault("name", model_key)
    merged.setdefault("model_type", "chat")
    if "params" not in merged or not isinstance(merged["params"], dict):
        merged["params"] = dict(merged.get("params") or {})

    provider = str(merged.get("provider") or "").strip().upper()
    if not provider:
        raise ValueError(
            f"Resolved LLM config {model_key!r} has no provider; "
            f"merge MODEL[{model_key!r}] with node chat_model or set provider on entry"
        )

    params = dict(merged.get("params") or {})
    model_name = params.pop("model", None)
    if not model_name:
        raise ValueError(f"MODEL[{model_key!r}] (or merged params) must set params.model")

    param_base_url = params.pop("base_url", None) or params.pop("api_base", None)
    if param_base_url and str(param_base_url).strip():
        api_base = str(param_base_url).strip()
    else:
        api_base = os.getenv(f"{provider}_BASE_URL")
    if not api_base:
        raise ValueError(
            f"Missing URL for model {model_key!r}. "
            f"Set MODEL.{model_key}.params.base_url or {provider}_BASE_URL in .env."
        )

    param_api_key = params.pop("api_key", None)
    if param_api_key and str(param_api_key).strip():
        api_key = str(param_api_key).strip()
    else:
        api_key = os.getenv(f"{provider}_API_KEY")
    if not api_key:
        raise ValueError(
            f"Missing API key for model {model_key!r}. "
            f"Set MODEL.{model_key}.params.api_key or {provider}_API_KEY in .env."
        )

    flat: dict[str, Any] = {
        "model": str(model_name),
        "api_base": api_base,
        "api_key": api_key,
    }
    flat.update(params)
    for k, v in merged.items():
        if k in _LLM_YAML_ONLY_KEYS or k in flat:
            continue
        flat[k] = v
    flat.setdefault("custom_llm_provider", "openai")
    return flat


def build_llm_configs_from_flex_config(config: dict[str, Any]) -> dict[str, Any]:
    """从 ``PRE_WORKFLOW`` / ``ACTOR_LOOP`` / ``POST_WORKFLOW`` + ``MODEL`` 构建 ``llm_configs`` 映射。"""
    model_section = config.get("MODEL") or {}
    if not isinstance(model_section, dict):
        model_section = {}

    llm_configs: dict[str, Any] = {}

    for section in ("PRE_WORKFLOW", "ACTOR_LOOP", "POST_WORKFLOW"):
        for node_cfg in config.get(section, []) or []:
            node_name = node_cfg.get("node", "")
            chat_model = node_cfg.get("chat_model")
            if not (node_name and chat_model):
                continue
            entry = chat_model if isinstance(chat_model, dict) else {"name": str(chat_model)}
            llm_configs[node_name] = resolve_llm_config_entry(model_section=model_section, entry=entry)

    for model_key, model_dict in model_section.items():
        if model_key in llm_configs:
            continue
        md = dict(model_dict) if isinstance(model_dict, dict) else {}
        md.setdefault("name", model_key)
        llm_configs[model_key] = resolve_llm_config_entry(model_section=model_section, entry=md)

    _merge_hook_llm_configs(config, llm_configs, model_section)
    return llm_configs


def _merge_hook_llm_configs(
    config: dict[str, Any],
    llm_configs: dict[str, Any],
    model_section: dict[str, Any],
) -> None:
    """为 HOOKS 字典项（含 ``name`` + ``model``）写入 ``llm_configs[hook_name]``。

    - **hook 的 ``name``**：合并后的 hook 标识（内置短名或 ``{suite_name}.hooks...`` 全路径），
      也是 ``llm_configs`` 里本条配置的键。
    - **``model``**：引用 ``MODEL`` 中**已存在**的槽名（与节点 ``chat_model`` 同源），**不得**写未在
      ``MODEL`` 中出现的键。合并后的扁平参数挂在 ``llm_configs[hook_name]``，供
      ``runtime.llm(hook_name)`` 使用（hook 内须使用与 YAML ``name`` 一致的键）。

    内置短名与 Suite 前缀 hook 均支持 ``model:``；不再要求 ``name`` 出现在内置 registry。

    仅处理 **字典** 形式的 HOOK 项；YAML 里写 ``- pruner`` 字符串的项不会进入本函数，见
    :meth:`dataagent.core.flex.agent.FlexAgent._register_hooks_from_config`。
    """
    hooks = config.get("HOOKS")
    if not isinstance(hooks, dict):
        return

    def _one(item: Any) -> None:
        if not isinstance(item, dict):
            return
        raw_model = item.get("model")
        if raw_model is None or raw_model == "":
            return
        if isinstance(raw_model, dict) and not raw_model:
            return
        if isinstance(raw_model, str) and not raw_model.strip():
            return
        hook_name = str(item.get("name") or "").strip()
        if not hook_name:
            raise ValueError("HOOKS: hook entry with 'model' must set 'name' (used as env.llm_configs key)")

        if hook_name in llm_configs:
            raise ValueError(
                f"HOOKS: hook name {hook_name!r} collides with existing llm_configs key "
                "(node name, MODEL slot, or another hook name)"
            )
        entry: dict[str, Any] | str
        if isinstance(raw_model, str):
            entry = raw_model.strip()
        elif isinstance(raw_model, dict):
            entry = dict(raw_model)
        else:
            entry = {"name": str(raw_model)}
        slot = entry.strip() if isinstance(entry, str) else str(entry.get("name") or entry.get("model") or "").strip()
        if not slot:
            raise ValueError("HOOKS: hook 'model' must name a MODEL slot (e.g. model: qwen3)")
        if slot not in model_section:
            raise ValueError(
                f"HOOKS: hook {hook_name!r} uses model {slot!r} which is not a key under MODEL. "
                f"Available: {sorted(model_section)}"
            )
        if isinstance(entry, dict):
            entry = dict(entry)
            entry.setdefault("name", slot)
        resolved_cfg = resolve_llm_config_entry(
            model_section=model_section,
            entry=entry,
            model_key_override=slot,
        )
        llm_configs[hook_name] = resolved_cfg

    agent_h = hooks.get("agent") or {}
    if isinstance(agent_h, dict):
        for phase in ("pre", "post"):
            for item in agent_h.get(phase) or []:
                _one(item)

    nodes_h = hooks.get("nodes") or {}
    if isinstance(nodes_h, dict):
        for _node_name, node_cfg in nodes_h.items():
            if not isinstance(node_cfg, dict):
                continue
            for phase in ("pre", "post"):
                for item in node_cfg.get(phase) or []:
                    _one(item)


def _optional_int_key(cfg: dict[str, Any], key: str) -> int | None:
    """AGENT_CONFIG 可选整型；缺省或显式 null 则为 None。"""
    if key not in cfg:
        return None
    raw = cfg.get(key)
    if raw is None or raw == "":
        return None
    return int(raw)


def _get_node_config_int(nodes: list[dict[str, Any]], node_name: str, key: str) -> int | None:
    """从节点配置中读取可选整型值。"""
    for node in nodes:
        if node.get("node") == node_name:
            raw = node.get(key)
            if raw is None or raw == "":
                return None
            return int(raw)
    return None


def build_agent_env_from_flex_config(
    config: dict[str, Any],
    mode: str = "",
    gym_env: Any = None,
    config_manager: Any | None = None,
) -> AgentEnv:
    """从 Flex YAML ``config`` 构建 :class:`AgentEnv`（含已解析的 ``llm_configs``）。

    Args:
        config: Merged Flex YAML configuration dict.
        mode: Scenario mode key for instructions lookup.
        gym_env: Optional gym environment whose tools are registered on the Agent ToolManager.
        config_manager: Per-Agent :class:`~dataagent.config.config_manager.ConfigManager` instance.
    """
    agent_cfg = config.get("AGENT_CONFIG", {}) or {}
    # 迭代上限：仅当 YAML 显式给出 max_iter 数值时生效；缺省为 None（不限制）
    max_iter = _optional_int_key(agent_cfg, "max_iter")
    hierarchy = str(agent_cfg.get("hierarchy", "MAIN") or "MAIN")
    token_limit = _optional_int_key(agent_cfg, "token_limit")

    # max_concurrency: 从节点配置读取
    all_nodes = config.get("ACTOR_LOOP", []) + config.get("PRE_WORKFLOW", []) + config.get("POST_WORKFLOW", [])
    max_concurrency = _get_node_config_int(all_nodes, "executor", "max_concurrency")

    instructions = ""
    scenario = config.get("SCENARIO", {}) or {}
    if isinstance(scenario, dict):
        if mode and isinstance(scenario.get(mode), dict):
            instructions = str(scenario[mode].get("instructions", "") or "").strip()
        if not instructions:
            for sc in scenario.values():
                if isinstance(sc, dict) and sc.get("instructions"):
                    instructions = str(sc["instructions"]).strip()
                    break

    llm_configs = build_llm_configs_from_flex_config(config)

    # bash 工具命令白名单：YAML 中 BASH_TOOL_WHITELIST 列表，未配置则为 None（不限制）
    bash_tool_whitelist: list[str] | None = None
    raw_whitelist = config.get("BASH_TOOL_WHITELIST")
    if raw_whitelist is not None and isinstance(raw_whitelist, list):
        bash_tool_whitelist = [str(cmd).strip() for cmd in raw_whitelist if str(cmd).strip()]

    # Phase 2: Create per-Agent ToolManager and initialize from config
    from dataagent.core.managers.action_manager.manager import ToolManager

    agent_tm = ToolManager(config_manager=config_manager)
    agent_tm.init_from_config(config)

    # Register gym Env tools into per-Agent ToolManager
    if gym_env is not None:
        for tool_name, tool_func in gym_env.get_tools().items():
            if not agent_tm.exists(tool_name):
                agent_tm.register_local_tool(tool_func, name=tool_name)

    # ── CONTEXT 压缩参数 ──
    context_cfg = config.get("CONTEXT", {}) or {}
    compress_token_limit = _optional_int_key(context_cfg, "compress_token_limit")
    compress_message_cnt = _optional_int_key(context_cfg, "compress_message_cnt")
    file_node_threshold = _optional_int_key(context_cfg, "file_node_threshold")

    environment_description = ""
    if gym_env is not None:
        get_desc = getattr(gym_env, "get_description", None)
        if callable(get_desc):
            try:
                environment_description = str(get_desc() or "").strip()
            except Exception:
                environment_description = ""

    return AgentEnv(
        llm_configs=llm_configs,
        tavily_configs={},
        modules={},
        hooks={},
        max_iter=max_iter,
        hierarchy=hierarchy,
        instructions=instructions,
        token_limit=token_limit,
        max_concurrency=max_concurrency,
        bash_tool_whitelist=bash_tool_whitelist,
        tool_manager=agent_tm,
        config_manager=config_manager,
        compress_token_limit=compress_token_limit,
        compress_message_cnt=compress_message_cnt,
        file_node_threshold=file_node_threshold,
        environment_description=environment_description,
    )


def build_runtime_from_flex_config(
    config: dict[str, Any],
    mode: str = "",
    gym_env: Any = None,
    config_manager: Any | None = None,
) -> Runtime:
    """从 Flex YAML ``config`` 构建 :class:`Runtime`（封装 ``build_agent_env_from_flex_config``）。"""
    return Runtime(build_agent_env_from_flex_config(config, mode=mode, gym_env=gym_env, config_manager=config_manager))
