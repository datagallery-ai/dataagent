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
import contextlib
import copy
import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml
from loguru import logger  # type: ignore[reportMissingImports]

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool.sandbox import get_current_sandbox
from dataagent.actions.tools.local_tool.tools import _resolve_bound_llm_model_name, sub_agent_tool
from dataagent.utils.constants import DEFAULT_SUBAGENT_TOOL_TIMEOUT
from dataagent.utils.runtime_paths import dataagent_package_root


async def document_recall_tool(
    query: str,
    document_paths: str | None = None,
    timeout: int = DEFAULT_SUBAGENT_TOOL_TIMEOUT,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """从多个 Markdown 文档中检索与查询语义相关的内容。

    适用场景：
    - 文档量级太大（几千行）无法全部注入主 Agent 上下文
    - 需要从业务规则、Schema 定义、术语表、领域知识等文档中提取语义信息
    - 复杂 NL2SQL 任务需要大量背景语义知识

    工作方式：
    1. 启动专用 subagent（只带 read_file + grep 工具）
    2. Subagent 先用 grep 关键词定位候选段落
    3. 再用 read_file 读取上下文，由 LLM 逐段判断相关性
    4. 编译结构化摘录结果返回

    Args:
        query (str): 描述需要检索的语义信息（自然语言）
        document_paths (str): 可选，文档路径。
        timeout (int): 超时秒数，默认 600

    Returns:
        {"original_msg": markdown 格式的召回结果, "frontend_msg": 摘要}
    """
    source_config_path = dataagent_package_root() / "agents" / "document_recall" / "document_recall_agent.yaml"
    with source_config_path.open(encoding="utf-8") as f:
        source_config = yaml.safe_load(f) or {}

    guard = get_current_sandbox()
    workspace_root = guard.workspace_root or Path.cwd().resolve()
    recall_id = uuid.uuid4().hex[:6]

    temp_config = _build_document_recall_sub_agent_config(
        source_config,
        config_manager=_tool_context.config_manager,
        tool_config=_tool_context.tool_config,
        workspace_root=workspace_root,
        recall_id=recall_id,
        allow_read_roots=guard.allow_read_roots,
    )

    # 构建增强 query：将文档路径信息注入 query 前缀
    enhanced_parts: list[str] = []
    if document_paths:
        path_lines = "\n".join(f"  - {p}" for p in document_paths)
        enhanced_parts.append(f"需要检索的文档列表：\n{path_lines}")
    main_documents = _tool_context.config_manager.get("DOCUMENTS")
    if isinstance(main_documents, dict):
        pre_paths = main_documents.get("paths") or []
        pre_globs = main_documents.get("globs") or []
        if pre_paths:
            path_lines = "\n".join(f"  - {p}" for p in pre_paths)
            enhanced_parts.append(f"预注册的文档路径：\n{path_lines}")
        if pre_globs:
            glob_lines = "\n".join(f"  - {g}" for g in pre_globs)
            enhanced_parts.append(f"预注册的文档 glob 模式：\n{glob_lines}")
    enhanced_query = "\n\n".join(enhanced_parts) + f"\n\n用户查询：{query}" if enhanced_parts else query

    temp_root = workspace_root
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix="document_recall_sub_agent_",
        dir=temp_root,
        delete=False,
        encoding="utf-8",
    ) as temp_file:
        yaml.safe_dump(temp_config, temp_file, allow_unicode=False, sort_keys=False)
        temp_config_path = temp_file.name

    try:
        res = await sub_agent_tool(query=enhanced_query, config_path=temp_config_path, timeout=timeout)
    finally:
        Path(temp_config_path).unlink(missing_ok=True)

    # sub_agent_tool 返回 {"original_msg": <worker_result>, "state": <flex state dict>, ...}
    # state 包含完整的 messages 列表，需提取最后一条 AIMessage 的 content 作为干净结果
    return _extract_recall_result_from_state(res)


def _build_document_recall_sub_agent_config(
    source_config: dict[str, Any],
    *,
    config_manager: Any,
    tool_config: dict[str, Any] | None = None,
    workspace_root: Path,
    recall_id: str,
    allow_read_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """构建 document_recall subagent 的临时配置，注入主 Agent 的模型配置和 workspace。"""
    temp_config = copy.deepcopy(source_config)

    bound_llm_model_name = _resolve_bound_llm_model_name(tool_config=tool_config)
    if bound_llm_model_name:
        runtime_model = config_manager.get(f"MODEL.{bound_llm_model_name}")
        if isinstance(runtime_model, dict) and runtime_model:
            runtime_model_copy = copy.deepcopy(runtime_model)
            runtime_params = runtime_model_copy.get("params")
            if isinstance(runtime_params, dict):
                runtime_params["temperature"] = 0.0
            temp_config["MODEL"] = {
                bound_llm_model_name: runtime_model_copy,
            }
            # 同步更新 ACTOR_LOOP 中 planner 的 chat_model.name
            actor_loop = temp_config.get("ACTOR_LOOP")
            if isinstance(actor_loop, list):
                for node_cfg in actor_loop:
                    if not isinstance(node_cfg, dict):
                        continue
                    chat_model = node_cfg.get("chat_model")
                    if isinstance(chat_model, dict):
                        chat_model["name"] = bound_llm_model_name

    # 直接使用主 Agent 的 workspace 配置覆盖，不保留子 YAML 中的 WORKSPACE
    ws: dict[str, Any] = {"path": str(workspace_root.resolve())}
    if allow_read_roots:
        ws["allow_path"] = [str(p) for p in allow_read_roots]
    temp_config["WORKSPACE"] = ws

    # 注入唯一 recall_id，供 subagent 工具做并发隔离：每次调用使用独立子目录
    temp_config["DOCUMENT_RECALL"] = {"run_id": recall_id}

    return temp_config


def _parse_str_message_content(msg_str: str) -> str | None:
    """从 str() 序列化的 LangChain 消息字符串中提取 content 字段值。

    ``_to_jsonable`` 对 Pydantic 模型回退到 ``str(obj)``，产出形如
    ``content='...' name='tool' tool_call_id='x'`` 的字符串。
    本函数用正则提取 content 值（兼容单/双引号包裹），返回 content 原始文本。"""
    m = re.search(r"content=([\"'])(.*?)\1\s+(?:name=|additional_kwargs=|tool_call_id=)", msg_str, re.DOTALL)
    return m.group(2) if m else None


def _find_tool_msg_in_strings(messages: list[Any], tool_name: str) -> str | None:
    """在（可能为字符串的）消息列表中查找指定名称的 ToolMessage，返回其 content。"""
    for msg in reversed(messages):
        msg_str = str(msg) if not isinstance(msg, str) else msg
        if f"name='{tool_name}'" in msg_str or f'name="{tool_name}"' in msg_str:
            content = _parse_str_message_content(msg_str)
            if content:
                return content
    return None


def _find_ai_msg_in_strings(messages: list[Any]) -> str | None:
    """在（可能为字符串的）消息列表中查找最后一条 AI 消息，返回其 content。"""
    for msg in reversed(messages):
        msg_str = str(msg) if not isinstance(msg, str) else msg
        # AI message: has content= and additional_kwargs=, no name= or tool_call_id=
        if "additional_kwargs=" in msg_str and "name=" not in msg_str and "tool_call_id=" not in msg_str:
            content = _parse_str_message_content(msg_str)
            if content:
                return content
    return None


def _extract_recall_result_from_state(res: dict[str, Any]) -> dict[str, Any]:
    """从 sub_agent_tool 返回的结果中提取召回结果。

    sub_agent_tool 返回 {"original_msg":..., "frontend_msg": ..., "state": <state dict>, "sub_id": ...}，
    其中 state 是完整的 Flex graph 最终状态（包含 messages 列表），original_msg 是 worker_result 结构体。

    返回格式：
    {
        "original_msg": str,   # 最终摘要文本 + 文件路径
        "frontend_msg": str,
        "data": {
            "recall_result_path": str,  # merge 后的 recall_result.json 绝对路径
            "num_entries": int,
        }
    }
    """
    frontend_msg = res.get("frontend_msg", "")

    # 从 worker_result 检查子 Agent 执行错误
    original_msg = res.get("original_msg")
    if isinstance(original_msg, dict) and original_msg.get("error"):
        err_msg = f"文档召回 Agent 执行失败：{original_msg['error']}"
        return {"original_msg": err_msg, "frontend_msg": err_msg}

    # 从 state 字段获取完整的 Flex graph 最终状态（包含 messages）
    state = res.get("state")
    if not isinstance(state, dict):
        logger.warning("document_recall_tool: state 不可用，回退为文本化输出")
        text = json.dumps(original_msg, ensure_ascii=False)
        return {"original_msg": text, "frontend_msg": frontend_msg or text}

    messages_raw = state.get("messages")
    if not isinstance(messages_raw, list) or not messages_raw:
        logger.warning("document_recall_tool: state dict 中未找到 messages，回退为文本化输出")
        text = json.dumps(state, ensure_ascii=False)
        return {"original_msg": text, "frontend_msg": frontend_msg or text}

    # 检查消息是 dict 还是 str（取决于 _to_jsonable 是否正确序列化了 Pydantic 模型）
    sample = messages_raw[0]
    if isinstance(sample, dict):
        messages = messages_raw  # Pydantic model_dump 路径 → dict 消息
    elif isinstance(sample, str):
        messages = messages_raw  # _to_jsonable str() 回退路径 → 字符串消息
    else:
        # LangChain 原生对象（不应该出现，以防万一）
        messages = [str(m) for m in messages_raw]

    # 1. 从 merge_recall_results 的 ToolMessage 中提取 output_path
    recall_path: str | None = None
    num_entries: int = 0

    # 先尝试 dict 路径
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("type") == "ToolMessage" and msg.get("name") == "merge_recall_results":
            content = str(msg.get("content", ""))
            break
    else:
        # 字符串路径：用正则从 str() 序列化的消息中提取
        content = _find_tool_msg_in_strings(messages, "merge_recall_results")

    if content:
        first_line = content.split("\n")[0]
        if first_line.startswith("召回结果已整合到 "):
            recall_path = first_line[len("召回结果已整合到 ") :].strip()
        for line in content.split("\n"):
            if line.startswith("共 ") and " 条摘录" in line:
                with contextlib.suppress(ValueError, IndexError):
                    num_entries = int(line.split("共 ")[1].split(" 条摘录")[0])

    # 2. 提取最后一条 AIMessage 的 content 作为摘要
    ai_content: str | None = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("type") == "AIMessage":
            c = msg.get("content", "")
            if c:
                ai_content = str(c)
                break
    if ai_content is None:
        ai_content = _find_ai_msg_in_strings(messages)

    # 3. 如果既没有 recall_path 也没有 ai_content，回退
    if not recall_path and not ai_content:
        logger.warning("document_recall_tool: 未找到 merge 结果或 AIMessage，回退为文本化输出")
        text = json.dumps(state, ensure_ascii=False)
        return {"original_msg": text, "frontend_msg": frontend_msg or text}

    # 4. 组装返回结果，显式提供文件路径
    result_msg = ""
    if recall_path:
        result_msg += f"文档召回结果文件：{recall_path}\n"
        if num_entries:
            result_msg += f"共召回 {num_entries} 条相关摘录\n"
    if ai_content:
        result_msg += f"\n{ai_content}"

    data: dict[str, Any] = {}
    if recall_path:
        data["recall_result_path"] = recall_path
    if num_entries:
        data["num_entries"] = num_entries

    return {
        "original_msg": result_msg.strip(),
        "frontend_msg": result_msg.strip(),
        "data": data if data else None,
    }
