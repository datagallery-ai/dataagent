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
"""Planner post-hook：交付完成度与确认贯穿性守卫（L3 + L4）。

本 hook 在 planner 置 complete=True 收尾前检查：
1. 交付配对（L3）：工作区 create_*/alter_* 建表脚本须有对应数据写入脚本
   （内容 INSERT INTO 目标表，或文件名 insert_*/modified_insert_* 同 stem 配对）；
   无任何交付 SQL 时要求补写；
2. 确认贯穿（L4）：hitl_auto_resolver 记录的已确认源表清单
   （workspace/.memory/hitl_confirmed_tables.json）中未被交付 SQL 引用的表，
   提醒 agent 核对是否遗漏。

增强（v41_r1 实证 NO_SQL 7 题中 6 题与"守卫拦截后仍继续检索/扩池→
死线烧穿"相关：q47/q58/q60/q68 timeout 型 + q40/q53 空文件穿透）：
- 硬指令：拦截提醒附"停止检索立即写 SQL / 直接修复勿再检索"的明确行动
  指令（此前提醒只说"请补写"，agent 仍回到检索循环）；第 2 次（最后一次）
  拦截加"最后提醒"措辞；
- fire 计数持久化：fires = max(runtime cache, .memory/delivery_guard_state.json)，
  修 chat 重入 runtime cache 重置漏拦/多拦（v41 q47 借重置多拦 1 次反多耗 70min）；
- 交付实质检查：内容为空或仅注释的 SQL 文件不算有效交付（q39/q53 空文件
  穿透"有文件"判定，守卫静默放行）。

命中任一检查 → complete=False，把提醒文本追加进最后一条 AIMessage 的
content（不能新增 HumanMessage 置尾——executor 要求 messages[-1] 为
AIMessage，否则抛异常；空 tool_calls 的 executor 轮为无害空转），回到
actor loop 补齐。每任务最多拦截 2 次（fire 上限，含持久化计数），之后
放行避免死循环。清单与比对只引用 agent 自己确认过的表，无 golden 泄漏。

仅在无人值守 HITL（enable_human_feedback + hitl_auto_resolve）场景生效。

注意：本文件具双重模块身份——运行期经 suite-root 单文件直载（hooks.yaml 条目
``hooks.delivery_guard.delivery_guard``），测试/工具经包路径 import。
禁止模块级 mutable 状态，不支持包内相对导入（见 suite hooks.yaml 头注释）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from loguru import logger

_GUARD_FIRE_COUNT_KEY = "delivery_guard.fire_count"
_GUARD_STATE_REL = ".memory/delivery_guard_state.json"
_MAX_GUARD_FIRES = 2
_MAX_TABLES_IN_REMINDER = 8

# 硬指令（v42 引入；v51 按纪律 0 优先级总纲重写）：拦截提醒必须给明确行动指令
# 与具体出口，且与 planner 纪律 0 同一条优先级链（交付义务 > 口径正确性 >
# 合规流程 > 效率偏好）——v50 实证守卫文案与纪律平级冲突时转化力崩塌
# （16 fire / 2 次打满仍零交付，q45/q46），三方文案必须一致。
_HARD_STOP_RETRIEVAL = (
    "行动要求（交付义务高于一切纪律，见纪律 0）：立即停止一切新的元数据检索/"
    "扩池检索——现在就写 SQL。用当前最优候选作数据源：已确认的表优先，其次池内"
    "与需求最接近的候选（禁止用已被明确拒绝的表），成对写出 create_*.sql 与"
    " insert_*.sql。不确定项、假设、与需求的差距逐条写进交付说明——这不是可以"
    "商量的建议：零交付是最差结果，不完美的交付远优于零交付。"
)
_HARD_FIX_DIRECT = (
    "行动要求（交付义务高于一切纪律，见纪律 0）：请直接修复上述问题后交付，"
    "不要为此再做任何新的元数据检索；修不完美的交付也远优于零交付。"
)
_LAST_WARNING = (
    "这是最后一次自动拦截，此后系统将直接放行结束任务——"
    "请务必现在就完成交付，不要再等待或检索。"
)

_CREATE_PREFIX_RE = re.compile(r"^(create_|alter_)", re.IGNORECASE)
_INSERT_PREFIX_RE = re.compile(r"^(modified_insert_|insert_)", re.IGNORECASE)
_CREATE_TABLE_RE = re.compile(
    r"create\s+(?:external\s+)?table\s+(?:if\s+not\s+exists\s+)?([a-zA-Z_][\w.]*)",
    re.IGNORECASE,
)
_INSERT_TARGET_RE = re.compile(
    r"insert\s+(?:into|overwrite)\s+(?:table\s+)?([a-zA-Z_][\w.]*)",
    re.IGNORECASE,
)
_SQL_REF_TABLE_RE = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)", re.IGNORECASE)
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def delivery_guard(
    state: Any,
    runtime: Any,
    *,
    original_state: Any = None,
) -> Any:
    """交付守卫：planner 收尾前核对交付配对（L3）与确认贯穿（L4）。"""
    result = state
    if not result.get("complete"):
        return result
    if not _is_unattended_hitl(result, original_state, runtime):
        return result

    source = original_state if original_state is not None else result
    workspace_root = _resolve_workspace_root(source, runtime)
    if workspace_root is None:
        return result
    # fire 计数持久化（v42）：runtime cache 会随 chat 重入重置，取两端最大值
    fires = max(_guard_fires(runtime), _read_state_fires(workspace_root))
    if fires >= _MAX_GUARD_FIRES:
        return result

    issues: list[str] = []
    delivery_files = _collect_delivery_sql_files(workspace_root)
    substantive = [p for p in delivery_files if _is_substantive_sql(p)]
    hollow = [p for p in delivery_files if p not in substantive]
    missing_inserts = _find_missing_inserts(substantive)
    if not delivery_files:
        issues.append(
            "工作区中未发现任何 create_*.sql / insert_*.sql 交付文件。"
            "请按任务要求产出建表与写入数据的 SQL 交付物（成对齐全）后再结束。"
        )
    if hollow:
        issues.append(
            "以下交付 SQL 文件内容为空或仅含注释，不构成有效交付："
            + "、".join(_display_name(p, workspace_root) for p in hollow)
            + "。请写入实际可执行的 SQL。"
        )
    if missing_inserts:
        issues.append(
            "以下建表脚本缺少对应的数据写入脚本："
            + "、".join(missing_inserts)
            + "。请补写相应的 insert_*.sql（INSERT INTO 目标表）后再交付。"
        )

    confirmed = _read_confirmed_tables(workspace_root)
    if confirmed:
        referenced = _referenced_tables(substantive)
        unused = [t for t in confirmed if _bare(t) not in referenced]
        if unused:
            issues.append(
                "以下已确认的数据源表未在交付 SQL 中使用："
                + "、".join(unused[:_MAX_TABLES_IN_REMINDER])
                + "。请对照任务需求核对是否遗漏；若确认无需使用，请说明理由后再交付。"
            )

    if not issues:
        return result

    fires += 1
    logger.warning(f"[delivery_guard] 交付守卫第 {fires}/{_MAX_GUARD_FIRES} 次拦截: {issues}")
    _set_guard_fires(runtime, fires)
    _write_state_fires(workspace_root, fires)
    instruction = _HARD_FIX_DIRECT if substantive else _HARD_STOP_RETRIEVAL
    updated_messages = _append_reminder(
        result.get("messages"), issues, instruction=instruction, last=fires >= _MAX_GUARD_FIRES
    )
    if updated_messages is None:
        return result
    return {**result, "messages": updated_messages, "complete": False}


def _is_unattended_hitl(
    result: Mapping[str, Any],
    original_state: Mapping[str, Any] | None,
    runtime: Any,
) -> bool:
    """仅无人值守 HITL（enable_human_feedback + hitl_auto_resolve）场景启用。"""
    source = original_state if original_state is not None else result
    enabled = source.get("enable_human_feedback")
    if enabled is None and runtime is not None:
        get_all_config = getattr(runtime, "get_all_config", None)
        if callable(get_all_config):
            try:
                config = get_all_config()
                agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, Mapping) else {}
                enabled = agent_config.get("enable_human_feedback", False)
            except Exception:
                enabled = False
    if not bool(enabled):
        return False
    from dataagent.core.flex.hooks.hitl_common import is_auto_resolve_enabled

    return is_auto_resolve_enabled(dict(source), None, runtime)


def _resolve_workspace_root(source: Mapping[str, Any], runtime: Any) -> Path | None:
    workspace = source.get("workspace") if isinstance(source, Mapping) else None
    if not workspace:
        workspace = getattr(runtime, "workspace_dir", None)
    if not workspace:
        return None
    try:
        return Path(str(workspace)).expanduser().resolve()
    except Exception:
        return None


def _guard_fires(runtime: Any) -> int:
    get_cache = getattr(runtime, "get_cache", None)
    if not callable(get_cache):
        return 0
    try:
        return int(get_cache(_GUARD_FIRE_COUNT_KEY, 0))
    except Exception:
        return 0


def _set_guard_fires(runtime: Any, count: int) -> None:
    set_cache = getattr(runtime, "set_cache", None)
    if callable(set_cache):
        set_cache(_GUARD_FIRE_COUNT_KEY, count)


def _read_state_fires(workspace_root: Path) -> int:
    """持久化 fire 计数（workspace 级）：chat 重入后 runtime cache 重置也不丢。"""
    try:
        payload = json.loads(
            (workspace_root / _GUARD_STATE_REL).read_text(encoding="utf-8")
        )
        return int(payload.get("fires", 0)) if isinstance(payload, dict) else 0
    except Exception:
        return 0


def _write_state_fires(workspace_root: Path, fires: int) -> None:
    try:
        state_path = workspace_root / _GUARD_STATE_REL
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"fires": int(fires)}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.debug(f"[delivery_guard] fire 计数持久化失败: {exc}")


def _is_substantive_sql(path: Path) -> bool:
    """SQL 文件是否含实质内容：剥离注释/空白/分号后非空（v42 空文件穿透修复）。"""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    stripped = _SQL_BLOCK_COMMENT_RE.sub(" ", _SQL_LINE_COMMENT_RE.sub(" ", content))
    return bool(re.sub(r"[\s;]", "", stripped))


def _display_name(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root))
    except ValueError:
        return path.name


def _collect_delivery_sql_files(workspace_root: Path) -> list[Path]:
    """收集工作区中的交付 SQL（create_/alter_/insert_/modified_insert_ 前缀）。"""
    files: list[Path] = []
    try:
        for path in sorted(workspace_root.rglob("*.sql")):
            if not path.is_file():
                continue
            if _CREATE_PREFIX_RE.match(path.name) or _INSERT_PREFIX_RE.match(path.name):
                files.append(path)
    except Exception as exc:
        logger.debug(f"[delivery_guard] workspace scan failed: {exc}")
    return files


def _find_missing_inserts(delivery_files: list[Path]) -> list[str]:
    """L3：建表脚本（内容建表名或文件名 stem）无对应 INSERT 目标即缺失。"""
    creates = [p for p in delivery_files if _CREATE_PREFIX_RE.match(p.name)]
    inserts = [p for p in delivery_files if _INSERT_PREFIX_RE.match(p.name)]
    if not creates:
        return []

    insert_targets: set[str] = set()
    for path in inserts:
        for target in _extract_tables(path, _INSERT_TARGET_RE):
            insert_targets.add(target)

    missing: list[str] = []
    for path in creates:
        created = _extract_tables(path, _CREATE_TABLE_RE)
        if created:
            if any(name in insert_targets for name in created):
                continue
        else:
            stem = _CREATE_PREFIX_RE.sub("", path.stem, count=1)
            if any(_INSERT_PREFIX_RE.sub("", p.stem, count=1) == stem for p in inserts):
                continue
        missing.append(path.name)
    return missing


def _extract_tables(path: Path, pattern: re.Pattern[str]) -> list[str]:
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        logger.debug(f"[delivery_guard] read failed ({path.name}): {exc}")
        return []
    return [_bare(m.group(1)) for m in pattern.finditer(content)]


def _read_confirmed_tables(workspace_root: Path) -> list[str]:
    path = workspace_root / ".memory" / "hitl_confirmed_tables.json"
    try:
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        tables = payload.get("confirmed_tables") if isinstance(payload, dict) else None
        if not isinstance(tables, list):
            return []
        return [t for t in tables if isinstance(t, str) and t.strip()]
    except Exception as exc:
        logger.debug(f"[delivery_guard] confirmed tables read failed: {exc}")
        return []


def _referenced_tables(delivery_files: list[Path]) -> set[str]:
    """交付 SQL 中 FROM/JOIN 引用的表（裸名小写），用于 L4 确认贯穿比对。"""
    referenced: set[str] = set()
    for path in delivery_files:
        referenced.update(_extract_tables(path, _SQL_REF_TABLE_RE))
    return referenced


def _bare(table: str) -> str:
    return str(table).strip().strip('`"\'').split(".")[-1].lower()


def _append_reminder(
    messages: Any, issues: list[str], *, instruction: str = "", last: bool = False
) -> list[Any] | None:
    """把提醒追加进最后一条 AIMessage 的 content。

    post-hook 收到的 result["messages"] 是节点增量输出：planner._build_result
    直接给单条 AIMessage（非列表）。不能追加新消息置尾（executor 要求
    messages[-1] 为 AIMessage），只能在最后一条 AI 消息 content 末尾续写。
    instruction 为硬行动指令（单独成段增强显著性）；last=True 时加最后提醒。
    """
    header = "【交付守卫提醒·最后提醒】" if last else "【交付守卫提醒】"
    reminder = header + "\n" + "\n".join(f"- {issue}" for issue in issues)
    if instruction:
        reminder += "\n\n" + instruction
    if last:
        reminder += "\n\n" + _LAST_WARNING
    if isinstance(messages, AIMessage):
        return [_append_to_ai_message(messages, reminder)]
    if isinstance(messages, list):
        for index in range(len(messages) - 1, -1, -1):
            if isinstance(messages[index], AIMessage):
                updated = _append_to_ai_message(messages[index], reminder)
                new_messages = list(messages)
                new_messages[index] = updated
                return new_messages
    return None


def _append_to_ai_message(message: AIMessage, reminder: str) -> AIMessage:
    content = message.content if isinstance(message.content, str) else str(message.content or "")
    return AIMessage(
        content=f"{content}\n\n{reminder}",
        additional_kwargs=dict(message.additional_kwargs or {}),
        response_metadata=dict(message.response_metadata or {}),
        id=message.id,
        name=message.name,
        tool_calls=list(message.tool_calls or []),
        invalid_tool_calls=list(message.invalid_tool_calls or []),
        usage_metadata=message.usage_metadata,
    )
