# ============================================================================
# 校验链路的「思考模式（thinking）」开关
#
# 校验链路的 LLM 调用都是「短提示 + 结构化输出」的任务，思考链只有负收益：
# 拉长响应、放大 token 消耗，还可能把 ``<think>`` 片段混进 JSON 正文。
# 所以这些调用默认关闭思考。
#
# 开关**按工具配置**控制（``tools.yaml`` → ``config.enable_thinking``），
# 不做全局控制 —— 改一个工具的配置不会影响其它链路，更不会影响主 Agent
# 的 planner 思考模式。
#
# ``enable_thinking`` 是 DashScope / 百炼（OpenAI 兼容模式）的扩展参数，
# 不是 OpenAI 标准字段。对不认识该字段的网关透传可能被拒（400），所以只在
# 「确认该网关认识它」时才下发：槽位自己开着（说明网关认识），或网关是百炼。
# ============================================================================
from __future__ import annotations

from typing import Any

#: 思考模式开关字段（DashScope / 百炼 OpenAI 兼容模式的扩展参数）。
#: 工具配置里用的键名也是它（``config.enable_thinking``）。
THINKING_FIELD = "enable_thinking"

# 百炼 / DashScope 的 endpoint 特征 —— 只有这些网关认识 ``enable_thinking``。
_BAILIAN_ENDPOINT_MARKERS: tuple[str, ...] = ("aliyuncs", "dashscope")


def _llm_client(llm: Any) -> Any:
    """取出底层 ``LLMClient``（适配器 → raw；本身就是 client 时返回自身）。"""
    raw = getattr(llm, "raw", None)
    return raw if raw is not None else llm


def _extra_body(llm: Any) -> dict[str, Any]:
    """模型槽位自带的请求体扩展参数（含 ``enable_thinking`` / ``temperature`` 等）。"""
    body = getattr(_llm_client(llm), "_extra_body", None)
    return dict(body) if isinstance(body, dict) else {}


def _api_base(llm: Any) -> str:
    """模型槽位的 endpoint 地址（小写）。"""
    client = _llm_client(llm)
    for attr in ("_api_base", "api_base", "base_url"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def supports_thinking_toggle(llm: Any) -> bool:
    """该模型槽位是否认识 ``enable_thinking`` 字段（``None`` 返回 ``False``）。"""
    if llm is None:
        return False
    # 槽位自己开着 → 这个网关肯定认识该字段。
    if _extra_body(llm).get(THINKING_FIELD) is True:
        return True
    return any(marker in _api_base(llm) for marker in _BAILIAN_ENDPOINT_MARKERS)


def thinking_enabled_from_config(config: Any) -> bool:
    """从工具配置（``tools.yaml`` 的 ``config`` 段）读取「是否允许模型思考」。

    缺省为 **False**（关闭）。非 dict / 缺键 / 非布尔非字符串一律按关闭处理。
    """
    if not isinstance(config, dict):
        return False
    value = config.get(THINKING_FIELD)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def resolve_thinking(tool_config: Any, explicit: bool | None = None) -> bool:
    """解析思考模式开关：显式参数优先，否则读工具配置（缺省关闭）。"""
    if explicit is not None:
        return bool(explicit)
    return thinking_enabled_from_config(tool_config)


def validation_llm_kwargs(llm: Any, enable_thinking: bool = False) -> dict[str, Any]:
    """校验链路 LLM 调用的 per-call kwargs。

    返回 ``{}`` 表示「不下发任何开关」，沿用模型槽位自身的配置 —— 这条降级
    是必要的：对不认识该字段的网关下发未知参数会被拒（400）。

    ``enable_thinking`` 走 per-call kwargs 而不是改模型槽位：per-call 参数在
    请求体组装时**覆盖**槽位配置，所以只影响这一次校验调用。
    """
    if not supports_thinking_toggle(llm):
        return {}
    return {THINKING_FIELD: bool(enable_thinking)}
