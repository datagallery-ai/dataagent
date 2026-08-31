# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""意图理解模板 Hook（Planner pre-hook）。

在 Planner 执行前，通过一次 LLM 调用完成槽位抽取与意图完整性判定：
- 填得满 → 渲染模板覆盖 user_query，Planner 正常执行
- 填不满 → 置 complete=True 短路，把缺口报告作为本轮回复

模板与字段声明从 runtime.config["INTENT_TEMPLATE"] 读取。
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState
from dataagent.core.framework_adapters.runtime.context import get_stream_writer
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate


def intent_understanding(state: FlexState, runtime: Runtime) -> FlexState:
    """Planner pre-hook：意图理解与槽位填充。

    流程：
    1. 读取 INTENT_TEMPLATE 配置（template + fields）
    2. 读取历史消息与当前 query
    3. 调用意图 LLM 完成槽位抽取 + 完整性判定 + 缺口报告
    4a. complete=True：渲染模板覆盖 user_query，写入 intent_slots
    4b. complete=False：置 complete=True，追加缺口 AIMessage

    Args:
        state: FlexState
        runtime: Runtime（含 llm 工厂与 config）

    Returns:
        修改后的 state
    """
    # 检查是否配置了意图理解（llm_configs 键为 hook 的 name）
    llm_configs = getattr(runtime.env, "llm_configs", {})
    if "intent_understanding" not in llm_configs:
        logger.debug("[intent_understanding] hook 未配置，跳过")
        return state

    config = runtime.get_config("INTENT_TEMPLATE")
    if not config:
        return state

    template_str = config.get("template", "")
    fields = config.get("fields", [])
    example = config.get("example", "")
    if not template_str or not fields:
        return state

    # 1. 准备 prompt
    messages = list(state.get("messages") or [])
    user_query = state.get("user_query", "")

    system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/intent_understanding/system")
    user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/intent_understanding/user")

    history_text = _format_history(messages)
    system_content = system_prompt.apply_prompt_template(fields=fields, template=template_str, example=example)
    user_content = user_prompt.apply_prompt_template(
        user_query=user_query,
        history=history_text,
        fields=", ".join(fields),
    )

    # 2. 调用 LLM（使用 hook name 查询 llm_configs）
    try:
        llm = runtime.llm("intent_understanding")
        resp = llm.invoke([SystemMessage(content=system_content), HumanMessage(content=user_content)])
        raw_content = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as e:
        logger.warning(f"[intent_understanding] LLM 调用失败: {e}")
        return state

    # 3. 解析 LLM 输出
    result = _parse_llm_response(raw_content)

    if result.get("complete"):
        # 4a. 填得满：渲染模板覆盖 user_query
        filled = result.get("filled", {})
        state["intent_complete"] = True
        state["intent_slots"] = filled
        state["missing_slots"] = []

        try:
            rendered = PromptTemplate.from_string(template_str).apply_prompt_template(**filled)
            state["user_query"] = rendered
        except Exception as e:
            logger.warning(f"[intent_understanding] 模板渲染失败: {e}")
            state["user_query"] = result.get("message", user_query)
    else:
        # 4b. 填不满：仅记录意图状态，由 Planner 作为正常 planner 结果返回缺口提示
        state["intent_complete"] = False
        state["intent_slots"] = result.get("filled", {})
        state["missing_slots"] = result.get("missing", [])
        message = result.get("message", "缺少必要信息，无法完成请求。")
        state["intent_missing_message"] = message

        _emit_missing_info_message(message)
        logger.info(f"[intent_understanding] 缺槽位，missing={result.get('missing')}, message={message[:80]!r}")

    return state


def _format_history(messages: list) -> str:
    """将历史消息格式化为文本。"""
    if not messages:
        return "（无历史消息）"

    lines = []
    for msg in messages[-10:]:  # 最多取最近 10 条
        role = msg.__class__.__name__.replace("Message", "").lower()
        content = getattr(msg, "content", "")
        if content:
            lines.append(f"**{role}**: {content}")
    return "\n".join(lines) if lines else "（无历史消息）"


def _parse_llm_response(raw: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON 响应。

    容错策略：JSON 不合法时退化为 complete=False + 原文当 message。
    """
    # 尝试提取 JSON 代码块
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if json_match:
        raw = json_match.group(1)

    # 尝试解析
    try:
        data = json.loads(raw.strip())
        return {
            "filled": data.get("filled", {}),
            "missing": data.get("missing", []),
            "complete": bool(data.get("complete", False)),
            "message": data.get("message", ""),
        }
    except json.JSONDecodeError:
        # 容错：直接用原文作为 message
        return {
            "filled": {},
            "missing": [],
            "complete": False,
            "message": raw.strip() or "意图理解失败，请重试。",
        }


def _emit_missing_info_message(message: str) -> None:
    writer = get_stream_writer()
    try:
        writer(
            {
                "type": "output_msg",
                "node_name": "DataAgent",
                "content": message,
                "reasoning_content": "",
            }
        )
    except Exception as exc:
        try:
            logger.debug(f"[intent_understanding] stream_writer 不可用，回退打印回复: {exc}")
        except Exception:
            logger.debug("rich 不可用，回退打印回复")
        try:
            from rich.console import Console

            Console().print(message)
        except Exception:
            logger.debug("rich 不可用，跳过打印回复")
