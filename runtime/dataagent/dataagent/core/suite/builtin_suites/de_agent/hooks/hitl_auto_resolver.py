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
"""HIL Auto Resolver Hook（de_agent suite，sql_feature_eval 无人值守场景）。

当 Agent 触发 HIL (request_human_feedback) 时，自动调用 LLM 生成回复：
- 从 messages[0] 获取原始 query
- 通过 JSON 文件匹配获取 golden_sql_path
- 调用 LLM 生成符合人类风格的回复
- 注入 ToolMessage 继续执行

匹配模式：
1. 直接模式（默认）：配置 hitl_task_index_path
   - 根据 query 在 JSON 中匹配获取 task
   - 直接读取 task.golden_sql_path 指定的文件
2. 目录匹配模式（兜底）：仅配置 hitl_golden_sql_dir
   - 在目录中匹配文件名包含 query 关键词的 .sql 文件

场景资产（action prompt/关键词表/口径语义表）在
:mod:`dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_resolver_assets`；
通用 HITL 基座（预算/去重/调用检测/消息注入/状态契约）在
:mod:`dataagent.core.flex.hooks.hitl_common`；分类机制在
:mod:`dataagent.core.flex.hooks.hitl_classifier`。

注意：本文件具双重模块身份——运行期经 suite-root 单文件直载（hooks.yaml 条目
``hooks.hitl_auto_resolver.hitl_auto_resolver``），测试/工具经包路径 import。
禁止模块级 mutable 状态（模块级常量与构造后不变更的对象除外），
不支持包内相对导入（见 suite hooks.yaml 头注释）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.hooks.hitl_classifier import HitlQuestionClassifier
from dataagent.core.flex.hooks.hitl_common import (
    HUMAN_FEEDBACK_TOOL_NAME,
    build_hook_return,
    extract_hil_question,
    extract_original_query,
    find_human_feedback_call,
    get_hitl_budget,
    get_hitl_question_history,
    get_hitl_turn_count,
    inject_fallback_reply,
    inject_simple_reply,
    inject_tool_message,
    is_auto_resolve_enabled,
    normalize_question,
    update_action_node,
)
from dataagent.core.flex.workflow.state import FlexState
from dataagent.core.suite.builtin_suites.de_agent.hooks.hitl_resolver_assets import (
    _ACTION_PROMPTS,
    _CALIBER_DECISION_RE,
    _CLAUSE_DELIM_RE,
    _CLAUSE_NEGATION_RE,
    _CLAUSE_QUESTION_RE,
    _COLUMN_ENUMERATION_ASK_PATTERN,
    _CONFIRM_DIRECTIVE_RE,
    _CONFIRMED_REFERENCE_MARKERS,
    _FIELD_CONCEPT_MAP,
    _HITL_AUTO_RESOLVER_PROMPT,
    _IDENTIFIER_FIELD_CANDIDATES,
    _TABLE_LAYER_PREFIXES,
    _TABLE_PROPOSAL_RE,
    _TABLE_PRONOUN_RE,
    _TABLE_SUFFIX_GRAINS,
    _TIME_RHS_TOKEN,
    _TIME_VAR_SEMANTICS,
    _VET_IDENTIFIER_FIELD_CONCEPT,
    _VET_ZH_CONCEPT_KEYWORDS,
    HITL_ACTION_KEYWORDS,
    HITL_ACTION_SPECS,
    HITL_DEFAULT_ACTION,
)


def _tables_are_confirmed_references(hil_question: str) -> bool:
    """问题中的表名是否为对已确认源表的引用（而非新候选提案）。"""
    return any(marker in hil_question for marker in _CONFIRMED_REFERENCE_MARKERS)


def _asks_caliber_decision(hil_question: str) -> bool:
    """提问的决策对象是否为口径/取数方式/标识选择。"""
    return bool(_CALIBER_DECISION_RE.search(hil_question or ""))


def _proposes_table_candidates(hil_question: str) -> bool:
    """提问是否在提出新的表候选（表名是待选对象而非上下文引用）。"""
    return bool(_TABLE_PROPOSAL_RE.search(hil_question or ""))


def _mixed_question_override(hil_question: str, base_action: str) -> str | None:
    """混合提问修正：命中 TABLE_CONFIRM 且含标识/时间口径关键词时的改判。

    - 全文提取不到候选表名 → 口径歧义问题；
    - 表名均为已确认引用 → 实际阻塞点在口径；
    - 口径决策提问且无新表候选提案表述（表名仅作上下文）→ 阻塞点在口径，
    均转 AMBIGUITY_CLARIFY。
    """
    if base_action != "TABLE_CONFIRM":
        return None
    if not any(kw.lower() in hil_question.lower() for kw in HITL_ACTION_KEYWORDS["AMBIGUITY_CLARIFY"]):
        return None
    tables_mentioned = _extract_agent_tables(hil_question)
    if not tables_mentioned or _tables_are_confirmed_references(hil_question):
        return "AMBIGUITY_CLARIFY"
    if _asks_caliber_decision(hil_question) and not _proposes_table_candidates(hil_question):
        return "AMBIGUITY_CLARIFY"
    return None


_HITL_CLASSIFIER = HitlQuestionClassifier(
    HITL_ACTION_SPECS,
    default_action=HITL_DEFAULT_ACTION,
    overrides=(_mixed_question_override,),
)


def classify_hitl_action(hil_question: str) -> str:
    """把 agent 提问映射到预定义 action，未命中则返回 UNA。

    优先级：OUTPUT_SCHEMA_PREVIEW > TABLE_CONFIRM > AMBIGUITY_CLARIFY > UNA
    输出结构问题优先匹配，因为输出相关问题常含"表名"指目标表，不能被 TABLE_CONFIRM 抢走。
    混合提问修正见 _mixed_question_override。
    """
    return _HITL_CLASSIFIER.classify(hil_question)


def _extract_golden_tables_from_sql(golden_sql: str) -> str:
    """从 golden SQL 中提取源表名（简单的 db.table 模式匹配）。"""
    if not golden_sql:
        return ""
    # 匹配 from/join 后面的 db.table 模式
    pattern = r"(?:from|join)\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)"
    matches = re.findall(pattern, golden_sql, re.IGNORECASE)
    # 去重保序
    seen = set()
    tables = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            tables.append(m)
    return ", ".join(tables) if tables else ""


# ── 程序化解析函数（程序化优先、LLM 兜底的基础）────────────────────────────


def _grain_concept_phrase(keys: list[str], hil_question: str) -> str:
    """聚合粒度键 → 客户口吻粒度短语（零泄漏边界内概念化）。

    - 键名（去表前缀）agent 在提问中自己提到 → 保留原名（复述 agent 的词允许）；
    - 未提到但可概念化（映射命中）→ 业务概念名；
    - 有任一键无法概念化 → 整组退化为通用短语，避免报出 agent 未知的字段名。
    """
    if not keys:
        return "任务原文要求的聚合主体"
    phrases: list[str] = []
    for key in keys:
        name = re.sub(r"^[a-z_][a-z0-9_]*\.", "", str(key).strip().strip("`\"' "), flags=re.IGNORECASE)
        name = name.strip("`\"' ").lower()
        if not name:
            return "任务原文要求的聚合主体"
        if re.search(rf"\b{re.escape(name)}\b", hil_question or "", re.IGNORECASE):
            phrases.append(name)
            continue
        concept = _FIELD_CONCEPT_MAP.get(name, "")
        if not concept:
            return "任务原文要求的聚合主体"
        phrases.append(concept)
    return "、".join(phrases)


def _asks_identifier_choice(hil_question: str) -> bool:
    """提问是否在选择/确认统计标识（而非顺带提及标识字段）。"""
    return bool(re.search(r"标识|维度|统计主体|用哪个|哪个字段|粒度", hil_question or ""))


def _extract_golden_tables_set(golden_sql: str) -> list[str]:
    """从 golden SQL 提取源表列表（去重保序，TABLE_CONFIRM 严格 diff 用）。

    识别 db.table 全名与裸数仓表名（dwd_/dim_/dws_/ods_/ads_ 前缀）——golden SQL
    不带 db 前缀时仍能得到非空集合，避免程序化判定/防泄漏/假确认审查三道防线
    静默失效。裸名限定数仓层前缀，防止把 CTE 名（with x as ...）误入 golden 集。
    """
    if not golden_sql:
        return []
    seen: set[str] = set()
    tables: list[str] = []
    for m in re.findall(r"(?:from|join)\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)", golden_sql, re.IGNORECASE):
        key = m.lower()
        if key not in seen:
            seen.add(key)
            tables.append(m)
    # 裸数仓表名，跳过已是某个 db.table 全名后缀的重复项
    for m in re.findall(r"(?:from|join)\s+((?:dwd|dim|dws|ods|ads)_[a-z0-9_]+)", golden_sql, re.IGNORECASE):
        key = m.lower()
        if key in seen:
            continue
        if any(full.endswith("." + key) for full in seen):
            continue
        seen.add(key)
        tables.append(m)
    return tables


def _extract_agent_tables(text: str) -> list[str]:
    """从 agent 提问文本（reason+pending_action）提取提到的表名。

    识别 db.table 全名与提问中显式出现的裸表名（dwd_/dim_/dws_/ods_/ads_ 前缀的数仓表）。
    中文与表名之间常无空格（"候选表biads.ads_x…dm已按"），Python 的 \\b 把中文算
    \\w，两端边界都会失配——用 ASCII 字符边界（(?<![a-z0-9_]) 等）替代 \\b。
    """
    if not text:
        return []
    tables: list[str] = []
    seen: set[str] = set()
    # db.table 全名
    for m in re.findall(
        r"(?<![a-z0-9_.])([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)(?![a-z0-9_])", text, re.IGNORECASE
    ):
        key = m.lower()
        if key not in seen:
            seen.add(key)
            tables.append(m)
    # 裸数仓表名（dwd_/dim_/dws_/ods_/ads_ 前缀），跳过已是某个 db.table 全名后缀的重复项
    for m in re.findall(
        r"(?<![a-z0-9_.])((?:dwd|dim|dws|ods|ads)_[a-z0-9_]+)(?![a-z0-9_])", text, re.IGNORECASE
    ):
        key = m.lower()
        if key in seen:
            continue
        if any(full.lower().endswith("." + key) for full in seen):
            continue
        seen.add(key)
        tables.append(m)
    return tables


def _tables_match(a: str, g: str) -> bool:
    """表名等价：精确相等或裸名后缀匹配（agent 口语常省 db 前缀）。

    已知边界：同裸名跨库（dwd.t1 / dim.t1）在此匹配下互相等价——泄漏方向
    安全（进泄漏判定的表均在 golden 集内），判定方向影响极小。
    """
    al, gl = a.lower(), g.lower()
    return al == gl or gl.endswith("." + al) or al.endswith("." + gl)


def _table_mentioned_in(table: str, text_low: str) -> bool:
    """text（已小写）中是否出现该表名：全名子串，或裸名（去 db 前缀）ASCII 边界出现。

    裸名用 ASCII 字符边界匹配（中文紧贴表名时 \\b 失效），避免 dwd_user_event
    误命中 dwd_user_event_v2 等更长表名。
    """
    tl = table.lower()
    if tl in text_low:
        return True
    bare = tl.rsplit(".", 1)[-1]
    if bare != tl:
        return bool(re.search(rf"(?<![a-z0-9_]){re.escape(bare)}(?![a-z0-9_])", text_low))
    return False


def _extract_golden_identifier_field(golden_sql: str) -> str:
    """检测 golden SQL 使用的用户标识字段（按在 SQL 中的词边界出现频次取最高）。"""
    if not golden_sql:
        return ""
    counts: dict[str, int] = {}
    for field in _IDENTIFIER_FIELD_CANDIDATES:
        # 词边界匹配，避免 device_id 误命中 device_id2
        n = len(re.findall(rf"\b{re.escape(field)}\b", golden_sql, re.IGNORECASE))
        if n:
            counts[field] = n
    if not counts:
        return ""
    return max(counts, key=counts.get)


def _infer_golden_calliber_traits(golden_sql: str) -> dict:
    """推断 golden 的口径特征：聚合粒度（GROUP BY 键）、时间口径、去重标志。

    供 AMBIGUITY_CLARIFY / OUTPUT_SCHEMA_PREVIEW 程序化转向使用；
    所有特征容错提取（无则空），不依赖完整 SQL 解析。
    """
    traits: dict[str, Any] = {}
    if not golden_sql:
        return traits
    # GROUP BY 键（第一个 group by 后到语句边界）
    gb = re.search(r"group\s+by\s+([^);]+)", golden_sql, re.IGNORECASE)
    if gb:
        keys = [k.strip() for k in gb.group(1).split(",") if k.strip()]
        traits["group_by_keys"] = keys
        # 聚合粒度里的标识字段（若有）
        for key in keys:
            for field in _IDENTIFIER_FIELD_CANDIDATES:
                if re.search(rf"\b{re.escape(field)}\b", key, re.IGNORECASE):
                    traits["grain_identifier"] = traits.get("grain_identifier") or field
    # 时间口径：分区字段与比较符
    time_field = ""
    for cand in ("pt_d", "dt", "day", "part_dt", "stat_date", "date"):
        if re.search(rf"\b{re.escape(cand)}\b\s*[=<>]", golden_sql, re.IGNORECASE):
            time_field = cand
            break
    if time_field:
        traits["time_field"] = time_field
    # 去重/序列拼接标志
    if re.search(r"\bdistinct\b", golden_sql, re.IGNORECASE):
        traits["has_distinct"] = True
    if re.search(r"row_number\s*\(\s*\)\s*over|concat\w*\s*\(", golden_sql, re.IGNORECASE):
        traits["has_row_number_or_concat"] = True
    return traits


# ── 时间窗口口径推断（golden 模板变量 → 客户口吻窗口描述）────────────────────
# golden SQL 的日期过滤用模板变量：'$date'=任务基准日（当天）、'${start_time}'=任务
# 指定起始日、'${start_time,-30,yyyyMMdd}'=基准日往前30天、'$last_seven_day'=7天前、
# DATE_SUB('$date_ep',N)=基准日往前N天。边界含/不含由操作符决定（>=/> 与 <=/<），
# 这正是 agent 高频出错点（含不含当天/起始日），故推断成具体窗口语义作答。


def _parse_time_var_token(token: str) -> tuple[str, int | None]:
    """解析时间模板变量 token，返回 (变量名, 天偏移或 None)。

    形如 '$date' / '${date}' / '${start_time,-30,yyyyMMdd}' / '$last_seven_day'。
    """
    t = token.strip("'\"")
    name_m = re.match(r"\$\{?([A-Za-z_]\w*)", t)
    if not name_m:
        return "", None
    off_m = re.search(r",\s*(-?\d+)\s*(?:,|})", t)
    offset = int(off_m.group(1)) if off_m else None
    return name_m.group(1), offset


def _time_boundary_phrase(op: str, name: str, offset: int | None) -> str:
    """单边界的客户口吻描述（含操作符的含/不含语义）。"""
    incl = "含" if op in (">=", "<=") else "不含"
    # 显式负偏移优先（DATE_SUB / ${start_time,-N} 归一后 name 是 date/start_time，
    # 但语义是"基准日往前 N 天"，不能按变量名直译成"当天/起始日"）
    if offset is not None and offset < 0:
        return f"基准日往前{-offset}天那天（{incl}）"
    if name in _TIME_VAR_SEMANTICS:
        label, _ = _TIME_VAR_SEMANTICS[name]
        return f"{label}（{incl}）"
    if offset == 0:
        return f"任务基准日（当天）（{incl}）"
    return f"该变量日期（{incl}）"


def _describe_golden_time_window(golden_sql: str, time_field: str) -> str:
    """推断 golden 时间过滤的窗口语义，返回客户口吻具体描述；解析不出返回 ""。

    支持单日快照、变量上下界区间、DATE_SUB 滚动窗口；一条 SQL 多处不同时间
    口径（明细表滚动+维表当天）时逐条列出。只描述业务口径，不报字段名。
    """
    if not golden_sql or not time_field:
        return ""
    # INSERT/DROP PARTITION 子句里的日期是写入目标分区，不是过滤条件，先剥除
    sql = re.sub(r"insert\s+overwrite\s+table\s+\S+\s+partition\s*\([^)]*\)", " ", golden_sql, flags=re.IGNORECASE)
    sql = re.sub(r"drop\s+(?:if\s+exists\s+)?partition\s*\([^)]*\)", " ", sql, flags=re.IGNORECASE)
    field = re.escape(time_field)

    conds: list[dict[str, Any]] = []
    # DATE_SUB 形式：field(>=|>) <fn...>(DATE_SUB(<base>, N))
    for m in re.finditer(
        rf"\b{field}\s*(>=|>|<=|<)\s*.{{0,80}}?date_?sub\s*\(\s*[^,()]+(?:\([^()]*\))?\s*,\s*(\d+)\s*\)",
        sql, re.IGNORECASE,
    ):
        conds.append({
            "side": "lo" if m.group(1) in (">=", ">") else "up",
            "op": m.group(1), "name": "date", "offset": -int(m.group(2)),
        })
    # 变量形式：field(=|>=|<=|>|<) '$var' / '${var[,offset,fmt]}'
    for m in re.finditer(rf"\b{field}\s*(=|>=|<=|>|<)\s*({_TIME_RHS_TOKEN})", sql, re.IGNORECASE):
        name, offset = _parse_time_var_token(m.group(2))
        if not name:
            continue
        conds.append({
            "side": "eq" if m.group(1) == "=" else ("lo" if m.group(1) in (">=", ">") else "up"),
            "op": m.group(1), "name": name, "offset": offset,
        })
    if not conds:
        return ""

    # 去重（同口径多处出现只算一次，如三张维表都取当天）
    deduped: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for c in conds:
        key = (c["side"], c["op"], c["name"], c["offset"])
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    phrases: list[str] = []
    # 上下界配对成区间（golden 里 lo/up 总是成对出现于同一 WHERE）
    los = [c for c in deduped if c["side"] == "lo"]
    ups = [c for c in deduped if c["side"] == "up"]
    for lo, up in zip(los, ups, strict=False):
        range_desc = (
            f"从{_time_boundary_phrase(lo['op'], lo['name'], lo['offset'])}"
            f"到{_time_boundary_phrase(up['op'], up['name'], up['offset'])}的全部日期分区"
        )
        # 两端均为天级偏移时给出总天数（"过去31天（含当天）"式口径）
        lo_off, up_off = lo["offset"], up["offset"]
        if up["name"] in _TIME_VAR_SEMANTICS:
            up_off = _TIME_VAR_SEMANTICS[up["name"]][1]
        if lo_off is not None and up_off == 0:
            lo_incl = lo["op"] == ">="
            up_incl = up["op"] == "<="
            span = -lo_off - 1 + (1 if lo_incl else 0) + (1 if up_incl else 0)
            if span > 0:
                range_desc += f"，即过去{span}天（含当天）" if up_incl else f"，共{span}天（不含当天）"
        phrases.append(range_desc)
    # 落单的下界：开放区间（从该日起到最新分区）
    for lo in los[len(ups):]:
        phrases.append(
            f"从{_time_boundary_phrase(lo['op'], lo['name'], lo['offset'])}起的所有日期分区（取到最新一天为止）"
        )
    # 等值条件：单日快照
    for c in deduped:
        if c["side"] != "eq":
            continue
        if c["name"] in ("date", "date_ep", "date_1", "date_2", "last_date"):
            phrases.append("仅取任务基准日（当天）这一个分区的单日快照，不做跨天滚动")
        elif c["name"] == "start_time":
            phrases.append("仅取任务指定的起始日那一个分区（单日快照）")
        else:
            phrases.append("仅取任务指定日期的单日快照，不做跨天滚动")
    if not phrases:
        return ""
    # 小时级分区补充
    if re.search(r"\bpt_h\s*=\s*['\"]?\$hour", sql, re.IGNORECASE):
        phrases.append("同一天内仅取任务指定的那个小时分区")
    if len(phrases) == 1:
        return phrases[0] + "。"
    listed = "；".join(f"{ordinal}，{p}" for ordinal, p in zip(("其一", "其二", "其三", "其四"), phrases, strict=False))
    return f"任务里存在多处不同的时间口径，请按各自数据源的用途对应：{listed}。"


def _extract_agent_column_count(text: str) -> int | None:
    """从 agent 提问文本提取其计划的输出列数（如"输出 8 列"/"8个字段"）。"""
    if not text:
        return None
    m = re.search(r"(?:输出|共|包含|计划|设计)[^0-9]{0,6}(\d{1,3})\s*(?:个?\s*(?:列|字段|column))", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,3})\s*(?:个?\s*(?:列|字段|column))", text)
    if m:
        return int(m.group(1))
    return None


def _count_golden_output_columns(golden_sql: str) -> int | None:
    """统计 golden 顶层 SELECT 输出列数（按第一个 select...from 块的顶层逗号计）。"""
    if not golden_sql:
        return None
    m = re.search(r"select\s+(.*?)\s+from\s", golden_sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    select_block = m.group(1)
    depth = 0
    commas = 0
    for ch in select_block:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            commas += 1
    if not select_block.strip():
        return None
    return commas + 1


def _extract_golden_schema_summary(golden_sql: str) -> str:
    """从 golden SQL 提取输出 schema 概要（SELECT 列 + 目标表）。"""
    if not golden_sql:
        return ""
    # 提取 SELECT 到 FROM 之间的列
    select_match = re.search(r"select\s+(.*?)\s+from", golden_sql, re.IGNORECASE | re.DOTALL)
    columns = select_match.group(1).strip() if select_match else ""
    # 提取目标表（INSERT OVERWRITE TABLE x）
    target_match = re.search(r"insert\s+overwrite\s+table\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)", golden_sql, re.IGNORECASE)
    target = target_match.group(1) if target_match else ""
    parts = []
    if target:
        parts.append(f"目标表: {target}")
    if columns:
        # 简化列展示，只取前 200 字符
        parts.append(f"SELECT 列: {columns[:200]}")
    return "\n".join(parts) if parts else ""


def _extract_relevant_sql_snippet(golden_sql: str, hil_question: str) -> str:
    """从 golden SQL 提取与提问相关的片段（简化版 AST 定位）。

    按 hil_question 关键词在 golden SQL 中定位相关行，返回该行 + 上下文 2 行。
    避免把整个 golden SQL 塞进 prompt（防止 leak）。
    """
    if not golden_sql or not hil_question:
        return ""
    lines = golden_sql.split("\n")
    # 提取 hil_question 中的关键词
    keywords = re.findall(r"[一-鿿]{2,}|[a-z_][a-z0-9_]{2,}", hil_question.lower())
    relevant_lines = []
    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(kw in line_lower for kw in keywords):
            # 加入该行 + 前后 2 行上下文
            start = max(0, i - 2)
            end = min(len(lines), i + 3)
            for j in range(start, end):
                if j not in relevant_lines:
                    relevant_lines.append(j)
    if not relevant_lines:
        # 没命中关键词，返回前 20 行作为 fallback
        return "\n".join(lines[:20])
    relevant_lines.sort()
    return "\n".join(lines[j] for j in relevant_lines)


def hitl_auto_resolver(
    state: FlexState,
    runtime: Runtime | None,
    *,
    original_state: FlexState | None = None,
) -> FlexState:
    """HIL 自动回复 Hook.

    新增机制：
    - Budget：每题总 budget=4，超预算强制收敛
    - 同题内去重：相同问题不重复回答
    - Action 分类：TABLE_CONFIRM/OUTPUT_SCHEMA_PREVIEW/AMBIGUITY_CLARIFY/UNA
    - UNA 拒绝：不当问题直接让 agent 自主决策

    触发条件：
    1. 配置了 hitl_auto_resolve: true
    2. 配置了 hitl_golden_sql_dir
    3. messages 中存在 request_human_feedback 工具调用

    Args:
        state: FlexState
        runtime: Runtime（含 llm 工厂与 config）
        original_state: 原始 state（优先从此处读取配置）

    Returns:
        修改后的 state（含注入的 ToolMessage）
    """
    if not is_auto_resolve_enabled(state, original_state, runtime):
        return state

    messages = state.get("messages", [])
    if not messages:
        return state

    # 1. 检测 HIL 调用
    feedback_call = find_human_feedback_call(messages)
    if feedback_call is None:
        logger.debug("[hitl_auto_resolver] 未找到 request_human_feedback 工具调用，跳过")
        return state

    # 2. 获取原始 query
    original_query = extract_original_query(messages)
    if not original_query:
        logger.warning("[hitl_auto_resolver] 无法获取原始 query，使用兜底回复")
        return inject_fallback_reply(state, messages, feedback_call, reason="no_original_query")

    logger.debug(f"[hitl_auto_resolver] 原始 query: {original_query[:80]!r}...")

    # 3. Budget 机制检查
    budget = get_hitl_budget(state, original_state)
    turn_count = get_hitl_turn_count(state, original_state)
    logger.info(f"[hitl_auto_resolver] budget={budget}, turn_count={turn_count}")

    if turn_count >= budget:
        # 超预算，强制收敛
        logger.warning(
            f"[hitl_auto_resolver] 已达交互预算上限（turn_count={turn_count} >= budget={budget}），强制收敛"
        )
        return inject_simple_reply(
            state, messages, feedback_call,
            "已达交互预算上限，请基于已有信息完成最终 SQL，不再询问。",
            action="BUDGET_EXHAUSTED",
            reason=f"turn_count={turn_count}>=budget={budget}",
        )

    # 4. 提取 HIL 问题
    hil_question = extract_hil_question(feedback_call)
    if not hil_question:
        logger.debug("[hitl_auto_resolver] 无法提取 HIL 问题内容，跳过")
        return inject_fallback_reply(state, messages, feedback_call, reason="no_hil_question")

    # 5. 同题内去重检查
    history = get_hitl_question_history(state, original_state, messages)
    hil_question_norm = normalize_question(hil_question)
    for item in history:
        if isinstance(item, dict) and item.get("question_norm") == hil_question_norm:
            logger.info(
                f"[hitl_auto_resolver] 同题内去重命中，复用历史回答: {item.get('answer', '')[:80]!r}"
            )
            return inject_simple_reply(
                state, messages, feedback_call,
                item.get("answer", "请基于已有信息自主判断，不要重复询问相同问题。"),
                action="DEDUP_HIT",
                reason="same as previous question",
            )

    # 6. Action 分类
    action = classify_hitl_action(hil_question)
    logger.info(f"[hitl_auto_resolver] action={action}, hil_question={hil_question[:100]!r}")

    # 7. UNA 类型直接拒绝，不走 LLM
    if action == "UNA":
        return inject_simple_reply(
            state, messages, feedback_call,
            _ACTION_PROMPTS["UNA"],
            action="UNA",
            reason="unclassified question",
        )

    # 8. 接近预算上限时给精简提示（不阻止 LLM，但提示 agent 收敛）
    if turn_count >= budget - 1:
        logger.warning(
            f"[hitl_auto_resolver] 接近预算上限（turn_count={turn_count} >= budget-1={budget-1}），给精简提示"
        )
        # 仍然走 LLM，但在 reply 后追加收敛提示

    # 9. 匹配 golden SQL（runtime config 精确直读优先，失败退回 tasks.json 模糊匹配）
    matched_task = None
    golden_sql_from_state, task_meta = _read_golden_sql_from_state(state, original_state, runtime)
    if golden_sql_from_state and task_meta:
        matched_task = task_meta
    else:
        matched_task = _try_match_golden_sql(original_query, state, original_state, runtime)
    if not matched_task:
        return inject_fallback_reply(state, messages, feedback_call, reason="golden_match_failed")

    # 10. 程序化优先，LLM 兜底，按 action 生成差异化回复
    result = _generate_and_inject_reply_v2(
        feedback_call, messages, original_query, matched_task, hil_question, action, state, runtime
    )
    if result is None:
        return inject_fallback_reply(state, messages, feedback_call, reason="reply_generation_failed")

    updated_messages, resolved_info = result

    # 11. 更新 turn_count 和 question_history
    resolved_info["action"] = action
    resolved_info["turn_count"] = turn_count + 1
    resolved_info["question_norm"] = hil_question_norm

    # 12. 更新 ActionNode 并构建返回值
    return build_hook_return(state, updated_messages, resolved_info)


def _try_match_golden_sql(
    original_query: str,
    state: Any,
    original_state: Any,
    runtime: Any,
) -> dict | None:
    """通过 JSON 索引匹配 golden SQL。"""
    task_index_path = _get_task_index_path(state, original_state, runtime)
    if not task_index_path:
        logger.debug("[hitl_auto_resolver] 未配置 hitl_task_index_path，跳过")
        return None

    golden_sql, matched_task = _match_golden_sql_by_path(original_query, task_index_path)
    if not golden_sql:
        logger.debug(f"[hitl_auto_resolver] JSON 索引未匹配到 golden SQL，query={original_query[:50]!r}")
        return None

    logger.debug(
        f"[hitl_auto_resolver] 匹配成功，task={matched_task.get('name')}, score={matched_task.get('score', 'N/A')}"
    )
    # golden_sql 内容随 matched_task 传递。
    # 下游 _generate_and_inject_reply_v2 若按相对路径重读失败会拿到空 SQL（盲答），
    # 此处已用 index_file.parent 正确解析过，直接嵌入避免二次读文件。
    matched_task = {**matched_task, "golden_sql": golden_sql, "_task_index_path": task_index_path}
    return matched_task


def _read_golden_sql_from_state(
    state: Any,
    original_state: Any,
    runtime: Any = None,
) -> tuple[str | None, dict | None]:
    """优先经 runtime config 直读 per-task 精确 golden（adapter 注入）。

    背景事实：
    - LangGraph TypedDict state 会静默丢弃 schema 外的输入键，adapter 塞进
      initial_state 的 golden 路径永远到不了 hook；
    - pre-hook 的 original_state 恒为 None（仅 post-hook 传入）。
    因此精确路径改走"每题独立的 runtime agent config"——adapter 在
    _write_runtime_agent_config 里写入 AGENT_CONFIG.hitl_golden_sql_path 等键，
    hook 经 runtime.get_all_config() 读到，天然 per-task 精确，无模糊匹配错题风险。

    Returns:
        (golden_sql, task_meta)；config 无路径或读取失败返回 (None, None)。
    """
    golden_sql_path = ""
    task_name = ""
    golden_schema_path = ""
    source = original_state if original_state is not None else state
    # 1) runtime config（每题独立，主通道）
    if runtime is not None:
        get_all_config = getattr(runtime, "get_all_config", None)
        if callable(get_all_config):
            try:
                config = get_all_config()
                agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, dict) else {}
                golden_sql_path = str(agent_config.get("hitl_golden_sql_path") or "").strip()
                task_name = str(agent_config.get("hitl_eval_task_name") or "").strip()
                golden_schema_path = str(agent_config.get("hitl_golden_schema_path") or "").strip()
            except Exception as e:
                logger.warning(f"[hitl_auto_resolver] 读取 runtime config 失败: {e}")
    # 2) state（兼容保留：若未来 state schema 开放了这些键）
    if not golden_sql_path:
        golden_sql_path = str(source.get("golden_sql_path") or "").strip()
        task_name = str(source.get("eval_task_name") or "").strip()
        golden_schema_path = str(source.get("golden_schema_path") or "").strip()
    if not golden_sql_path:
        return None, None
    sql_file = Path(golden_sql_path)
    if not sql_file.is_file():
        logger.warning(f"[hitl_auto_resolver] 精确 golden SQL 文件不存在: {sql_file}")
        return None, None
    try:
        golden_sql = sql_file.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 读取精确 golden SQL 失败: {e}")
        return None, None
    if not golden_sql:
        return None, None
    task_meta = {
        "name": task_name,
        "golden_sql_path": golden_sql_path,
        "golden_schema_path": golden_schema_path,
        "golden_sql": golden_sql,
    }
    logger.info(f"[hitl_auto_resolver] 精确 golden 直读成功（task={task_name}），跳过模糊匹配")
    return golden_sql, task_meta


def _customer_restate_query(original_query: str, limit: int = 80) -> str:
    """客户口吻的需求复述素材：截断原始 query，只作复述用。"""
    text = re.sub(r"\s+", " ", (original_query or "")).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _table_rejection_reply(correct: list[str], wrong: list[str], query_head: str) -> str:
    """源表判错的客户口吻拒绝模板（程序化分支与 LLM 兜底防线共用）。"""
    parts = []
    if correct:
        parts.append(f"{'、'.join(correct)} 这几张表符合我的需求，就此定下来，不用再就这几张表来问我。")
    restate = f"我的需求是：{query_head}。" if query_head else ""
    wrong_lead = "但 " if correct else ""
    parts.append(
        f"{wrong_lead}{'、'.join(wrong)} 从我任务的需求看不是我想要的数据来源。{restate}"
        "请你基于元数据再检索一下（换用需求里的业务词重新检索，不要原样重复刚才的候选），"
        "给我 2-3 个候选数据源选项（说明各自记录的数据和适用场景），我来选。"
    )
    return "".join(parts)


def _prior_rejection_rounds(messages: list | None) -> int:
    """同题内已被客户拒绝的源表确认轮数（按拒绝模板标记语统计）。"""
    if not messages:
        return 0
    return sum(
        1
        for msg in messages
        if isinstance(msg, ToolMessage)
        and getattr(msg, "name", "") == HUMAN_FEEDBACK_TOOL_NAME
        and "不是我想要的数据来源" in str(msg.content)
    )


def _escalated_table_rejection_reply(
    golden_sql: str,
    hil_question: str,
    wrong: list[str],
    prior_rejects: int,
) -> str | None:
    """检索-拒绝死局的升级答复：给出需求侧数据特征（层级/粒度/窗口）引导换轴检索。

    触发条件（连续选错源表）：前一轮已被拒绝且本轮候选仍全错。重复同一句
    "不是我要的数据来源"只会让 agent 在同一个候选池里打转（golden 表可能
    根本不在其检索候选池）；升级为特征级指引给 agent 新的检索轴。
    零泄漏边界：只描述业务特征，不报 db 名/表名/字段名。
    特征全部解析不出时返回 None，落回标准拒绝模板。
    """
    golden_tables = _extract_golden_tables_set(golden_sql)
    feats: list[str] = []
    layers: list[str] = []
    grains: list[str] = []
    for t in golden_tables:
        name = t.split(".")[-1].lower()
        for pref, label in _TABLE_LAYER_PREFIXES:
            if name.startswith(pref) and label not in layers:
                layers.append(label)
                break
        for suf, label in _TABLE_SUFFIX_GRAINS:
            if name.endswith(suf) and label not in grains:
                grains.append(label)
    if layers:
        feats.append(f"数据所在数仓层级：{'、'.join(layers)}")
    if grains:
        feats.append(f"数据时间粒度：{'、'.join(grains)}")
    traits = _infer_golden_calliber_traits(golden_sql)
    gb_keys = traits.get("group_by_keys")
    if gb_keys:
        feats.append(f"每行记录粒度：{_grain_concept_phrase(gb_keys, hil_question)}（一行对应一个该粒度主体）")
    time_field = traits.get("time_field")
    if time_field:
        window = _describe_golden_time_window(golden_sql, time_field)
        if window:
            feats.append(f"数据时间窗口：{window}")
    if not feats:
        return None
    return (
        f"{'、'.join(wrong)} 仍然不是我想要的数据来源——前面 {prior_rejects} 轮你给的候选都不符合，"
        "这次我把需求特征说得更具体些，你按这些特征去检索："
        + "；".join(feats)
        + "。请用这些业务特征（而不是刚才的表名）重新检索元数据，给我候选；"
        "若仍检索不到完全匹配的，选择与这些特征最接近的候选表，向我说明差距后继续推进。"
    )


def _answer_table_confirm_programmatic(
    golden_sql: str,
    hil_question: str,
    original_query: str = "",
    messages: list | None = None,
) -> str | None:
    """TABLE_CONFIRM：客户口吻零泄漏源表判定（程序化）。

    角色约束：resolver 扮演任务提出方（客户）——只知道自己业务需要什么数据，
    不知道数仓里有哪些表。golden 源表集合仅用于内部判定（对/错/缺），
    回复中只允许出现 agent 自己提到的表名 + 需求复述，禁止报出 golden 表名，
    否则 HITL 会变成答案通道、架空 agent 的 schema linking 能力（评测核心指标）。

    分支：
    - 连续选错（前轮已拒 + 本轮候选仍全错）→ 升级为特征级指引
      （层级/粒度/窗口，见 _escalated_table_rejection_reply），打破检索-拒绝死局；
    - agent 提到错表 → 客户口吻拒绝错表（需求角度），要求给 2-3 个候选选项；
    - agent 提到的表全对但 golden 还有缺 → 只确认 agent 的表，让 agent 对照
      需求自查是否遗漏数据（不报缺哪些表）；
    - agent 提到的表与 golden 一致 → 确认 agent 的表 + 推进锚；
    - agent 没提表名 → 要求 agent 基于元数据给出候选选项（固定模板，不走 LLM）。
    """
    golden_tables = _extract_golden_tables_set(golden_sql)
    if not golden_tables:
        logger.warning(
            "[hitl_auto_resolver] golden SQL 未提取到源表（可能无 db. 前缀），"
            f"TABLE_CONFIRM 程序化判定退回 LLM 兜底: {golden_sql[:80]!r}"
        )
        return None
    query_head = _customer_restate_query(original_query)
    agent_tables = _extract_agent_tables(hil_question)
    if not agent_tables:
        return (
            "数据源选择这个事我需要你先做功课：请基于元数据检索后，"
            "给我 2-3 个候选数据源选项（说明每张表记录的是什么数据、适用场景），"
            "我来从中确认符合我需求的。"
        )
    # 裸名/限定名混写容忍（agent 口语常省 db 前缀，golden SQL 可能不带前缀）
    wrong = [t for t in agent_tables if not any(_tables_match(t, g) for g in golden_tables)]
    missing = [g for g in golden_tables if not any(_tables_match(a, g) for a in agent_tables)]
    correct = [t for t in agent_tables if any(_tables_match(t, g) for g in golden_tables)]
    if wrong:
        # 连续选错：升级为特征级指引（给新检索轴），避免同一句拒绝让 agent
        # 在原候选池打转（golden 表可能根本不在其检索候选池里）
        if not correct:
            prior_rejects = _prior_rejection_rounds(messages)
            if prior_rejects >= 1:
                escalated = _escalated_table_rejection_reply(golden_sql, hil_question, wrong, prior_rejects)
                if escalated:
                    return escalated
        return _table_rejection_reply(correct, wrong, query_head)
    if missing:
        return (
            f"{'、'.join(agent_tables)} 这些表符合我的需求，就此定下来——"
            "不用再就这些表来问我，请直接基于它们推进下一步（该核对字段口径或写 SQL 了）。"
            "推进的同时请对照我的任务需求自查一下是否还有遗漏的数据"
            "（比如是否还需要其他类型的明细数据或维表）；"
            "如果确需补充数据源，给我候选选项，我来确认。"
        )
    return (
        f"确认：就用 {'、'.join(agent_tables)}。数据源这块可以定了，"
        "接下来如需确认输出结构或统计口径，整理成具体选项再来问我。"
    )


def _sanitize_llm_reply_leak(answer: str, golden_sql: str, hil_question: str, action: str) -> str:
    """防泄漏护栏：LLM 回复中出现 agent 未提到的 golden 表名即视为答案泄漏。

    客户角色不知道数仓表名；golden 表名被 LLM 报出 = 替 agent 做了 schema linking。
    - TABLE_CONFIRM：整体替换为客户口吻固定模板（要求给候选选项）；
    - 其他 action：将泄漏表名剥除为"符合我需求的数据表"占位。
    agent 自己在提问里提到的表名不构成泄漏（客户复述 agent 的选项是允许的）。
    裸名/全名双向匹配：agent 裸提（省 db 前缀）时 LLM 全名复述不算泄漏；
    LLM 裸名报出 golden 表（agent 未提过）同样构成泄漏（见 _tables_match/_table_mentioned_in）。
    """
    golden_tables = _extract_golden_tables_set(golden_sql)
    if not golden_tables:
        logger.warning(
            "[hitl_auto_resolver] golden SQL 未提取到源表（可能无 db. 前缀），防泄漏护栏已降级放行"
        )
        return answer
    agent_tables = _extract_agent_tables(hil_question)
    ans_low = (answer or "").lower()
    leaked = [
        t for t in golden_tables
        if not any(_tables_match(t, a) for a in agent_tables)
        and _table_mentioned_in(t, ans_low)
    ]
    if not leaked:
        return answer
    logger.warning(f"[hitl_auto_resolver] LLM 回复泄漏 golden 表名（action={action}），已拦截: {leaked}")
    if action == "TABLE_CONFIRM":
        return (
            "数据源选择我需要你先做功课：请基于元数据检索后，"
            "给我 2-3 个候选数据源选项（说明每张表记录的是什么数据、适用场景），"
            "我来从中确认符合我需求的。"
        )
    sanitized = answer
    for t in leaked:
        tl = t.lower()
        bare = tl.rsplit(".", 1)[-1]
        forms = (t, bare) if bare != tl else (t,)
        for form in forms:
            sanitized = re.sub(
                rf"(?<![a-z0-9_]){re.escape(form)}(?![a-z0-9_])",
                "符合我需求的数据表",
                sanitized,
                flags=re.IGNORECASE,
            )
    return sanitized


# ── LLM 兜底假确认防线 ──────────────────────────────────────────────────────
# 程序化分支未覆盖而落到 LLM 兜底时，LLM 有迎合 agent 的假确认
# （"使用 device_id 作为用户标识"而 golden 实为 user_id；agent 全部源表候选
# 都错时仍"同意使用该表"）。防线按子句审查 LLM 回复：确认内容与 golden 实质
# 冲突 → 改写为客户口吻纠正；疑问/否定/无 golden 信号时不动原文。


def _split_clauses(text: str) -> list[str]:
    """按句读切分子句（保留分隔符），供防线逐子句审查。"""
    parts = _CLAUSE_DELIM_RE.split(text)
    chunks: list[str] = []
    for i in range(0, len(parts), 2):
        clause = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if clause or delim:
            chunks.append(clause + delim)
    return chunks


def _clause_identifier_concepts(clause: str) -> set[str]:
    """子句中出现的标识体系概念集合。"""
    low = clause.lower()
    found: set[str] = set()
    for field, concept in _VET_IDENTIFIER_FIELD_CONCEPT.items():
        if re.search(rf"\b{re.escape(field)}\b", low):
            found.add(concept)
    for kw, concept in _VET_ZH_CONCEPT_KEYWORDS:
        if kw in low:
            found.add(concept)
    return found


def _is_confirm_directive_clause(clause: str) -> bool:
    """子句是否为"确认/指示采用 X"的陈述（疑问与否定子句不算）。"""
    if not clause.strip():
        return False
    if _CLAUSE_QUESTION_RE.search(clause):
        return False
    if _CLAUSE_NEGATION_RE.search(clause):
        return False
    return bool(_CONFIRM_DIRECTIVE_RE.search(clause))


def _identifier_redirect_clause(golden_concept: str, wrong_concepts: set[str]) -> str:
    """标识体系假确认的概念化纠正子句（不报 golden 字段名）。"""
    wrong_txt = "、".join(sorted(wrong_concepts))
    return (
        f"标识字段这里我要纠正：本场景的统计主体是{golden_concept}这一类"
        f"（一行对应一个{golden_concept}），不是{wrong_txt}。"
        f"请回到源表字段清单，按字段说明核对对应{golden_concept}的字段"
        "（注意同名字段可能存在主标识/二代标识等变体），确认后再使用。"
    )


def _vet_table_offending_tables(
    clause: str,
    golden_tables: list[str],
    agent_tables: list[str],
) -> list[str] | None:
    """子句若在确认错误源表，返回被错误确认的表；否则 None。

    - 显式表名：子句内出现的表全部不在 golden（部分正确时指代不明，保守不动）；
    - 代词确认（该表/这些表）：仅当提问中的 agent 候选全部不在 golden 时才判。
    裸表名与 golden 全名按后缀等价匹配（agent 口语常省 db 前缀，见 _tables_match）。
    """
    def _in_golden(t: str) -> bool:
        return any(_tables_match(t, g) for g in golden_tables)

    clause_tables = _extract_agent_tables(clause)
    if clause_tables:
        wrong = [t for t in clause_tables if not _in_golden(t)]
        if wrong and len(wrong) == len(clause_tables):
            return wrong
        return None
    if _TABLE_PRONOUN_RE.search(clause):
        wrong_all = [t for t in agent_tables if not _in_golden(t)]
        if agent_tables and len(wrong_all) == len(agent_tables):
            return wrong_all
    return None


def _vet_llm_confirmation(
    answer: str,
    golden_sql: str,
    hil_question: str,
    action: str,
    original_query: str = "",
) -> str:
    """假确认防线：审查 LLM 兜底回复，改写与 golden 实质冲突的"确认"子句。

    - 标识体系：子句确认的标识体系 ≠ golden 标识体系 → 替换为概念化纠正；
    - 源表：agent 候选全错仍被"同意使用该表" → 替换为拒绝模板；
    - 同类冲突只纠正一次，其余冲突子句丢弃（避免重复纠正语）。
    无 golden 信号（无标识/无源表可判）时原样返回。
    """
    if not answer:
        return answer
    golden_id = _extract_golden_identifier_field(golden_sql)
    golden_concept = _FIELD_CONCEPT_MAP.get(golden_id.lower(), "") if golden_id else ""
    golden_tables = _extract_golden_tables_set(golden_sql)
    agent_tables = _extract_agent_tables(hil_question)
    if not golden_tables:
        logger.warning(
            "[hitl_auto_resolver] golden SQL 未提取到源表（可能无 db. 前缀），假确认审查的源表防线已降级"
        )
    if not golden_concept and not golden_tables:
        return answer
    query_head = _customer_restate_query(original_query)
    out: list[str] = []
    id_redirected = False
    table_redirected = False
    for clause in _split_clauses(answer):
        if _is_confirm_directive_clause(clause):
            if golden_concept:
                concepts = _clause_identifier_concepts(clause)
                if concepts and golden_concept not in concepts:
                    if not id_redirected:
                        out.append(_identifier_redirect_clause(golden_concept, concepts))
                        id_redirected = True
                        logger.info(
                            f"[hitl_auto_resolver] LLM 假确认防线：标识体系纠正（子句概念 {sorted(concepts)} 与 golden 不符）"
                        )
                    continue
            if golden_tables:
                wrong_tables = _vet_table_offending_tables(clause, golden_tables, agent_tables)
                if wrong_tables:
                    if not table_redirected:
                        out.append(_table_rejection_reply([], wrong_tables, query_head))
                        table_redirected = True
                        logger.info(
                            f"[hitl_auto_resolver] LLM 假确认防线：源表假确认纠正（被错误确认: {wrong_tables}）"
                        )
                    continue
        out.append(clause)
    return "".join(out)


def _answer_output_schema_programmatic(
    golden_sql: str,
    hil_question: str,
) -> str | None:
    """OUTPUT_SCHEMA_PREVIEW：列数对比 + 聚合粒度转向（程序化优先）。

    评测口径"写入数据等价即 PASS"不扣列别名差异，故聚焦列数与聚合粒度；
    解析不出信号返回 None 退回 LLM 兜底。
    """
    traits = _infer_golden_calliber_traits(golden_sql)
    agent_cols = _extract_agent_column_count(hil_question)
    golden_cols = _count_golden_output_columns(golden_sql)
    # 1) 列数对比（agent 明说了列数且 golden 列数可解析）
    if agent_cols is not None and golden_cols is not None and agent_cols != golden_cols:
        return (
            f"输出列数需要修正：期望输出 {golden_cols} 列，你计划输出 {agent_cols} 列，"
            f"请按任务要求的输出结构（{golden_cols} 列）调整。"
        )
    # 2) 聚合粒度转向（对应"37 列 vs 8 列"明细宽表失败模式）；
    #    粒度键概念化（agent 未提到的字段名不报，见 _grain_concept_phrase）
    grain_parts: list[str] = []
    grain_id = traits.get("grain_identifier")
    gb_keys = traits.get("group_by_keys")
    if gb_keys:
        grain_parts.append(f"按 {_grain_concept_phrase(gb_keys, hil_question)} 聚合")
    elif grain_id:
        grain_parts.append(f"按 {_grain_concept_phrase([grain_id], hil_question)} 粒度聚合")
    if grain_parts:
        hint = (
            f"输出应为{'，'.join(grain_parts)}后的汇总结果（单 key 单行）"
            if traits.get("group_by_keys")
            else f"输出应为{'，'.join(grain_parts)}的汇总结果（单 key 单行）"
        )
        if traits.get("has_distinct") or traits.get("has_row_number_or_concat"):
            hint += "，序列字段需先去重再拼接"
        if golden_cols is not None:
            hint += f"，期望输出 {golden_cols} 列"
        hint += "。不是逐明细的宽表，请确认你的输出粒度与此一致。"
        return hint
    # 3) 仅有列数信息
    if golden_cols is not None:
        return f"期望输出 {golden_cols} 列。请确认你的输出列数与任务要求一致后继续。"
    return None


def _is_column_enumeration_ask(hil_question: str) -> bool:
    """判断是否为"要求用户枚举输出列清单"的提问。"""
    return bool(_COLUMN_ENUMERATION_ASK_PATTERN.search(hil_question or ""))


def _answer_ambiguity_clarify_programmatic(
    golden_sql: str,
    hil_question: str,
) -> str | None:
    """AMBIGUITY_CLARIFY：标识字段/时间口径回答（程序化优先，零泄漏）。

    混合提问（标识 + 时间在同一问里）给组合答复，两部分口径分别确认。
    - agent 在标识字段间纠结且 golden 标识恰在选项中 → 客户从选项中选定；
    - golden 标识不在 agent 选项中 → 概念化转向：报业务概念粒度（如"账号ID"），
      不报字段名——此前交给 LLM 兜底时实测有顺着 agent 假确认的迎合风险；
    - agent 问时间口径 → 按 golden 特征给口径提示；
    - 标识与时间都解析不出 → 返回 None 退回 LLM 兜底。
    """
    golden_id = _extract_golden_identifier_field(golden_sql)
    # 提问中出现的标识字段（词边界）
    mentioned = [
        f for f in _IDENTIFIER_FIELD_CANDIDATES
        if re.search(rf"\b{re.escape(f)}\b", hil_question, re.IGNORECASE)
    ]
    # Mid 阶段混合提问（"标识字段用哪个 + 时间窗口怎么算"）改成组合答复——
    # 两部分口径分别确认，避免先命中的分支提前 return 把另一半问题晾着不答。
    parts: list[str] = []
    # 仅当 golden 标识字段是 agent 提供的选项之一时才直接选定（客户从选项中选）；
    # golden 字段不在选项中时不报字段名（泄漏），改报业务概念粒度把字段核对
    # 交还给 agent（注意同名字段常有主标识/二代标识等变体）。
    if golden_id and mentioned and golden_id in mentioned:
        parts.append(f"确认：用户标识使用 {golden_id}。")
    elif golden_id and mentioned and _asks_identifier_choice(hil_question):
        grain = _FIELD_CONCEPT_MAP.get(golden_id.lower(), "") or "任务统计主体"
        parts.append(
            "标识字段我不能从你给的选项里直接确认——这几个都不是本场景的统计主体。"
            f"本场景按{grain}粒度统计（一行对应一个{grain}）。"
            "请回到源表字段清单，按字段说明核对哪个字段对应这一粒度"
            "（注意同名字段可能存在主标识/二代标识等变体），确认后再使用。"
        )
    # 时间口径（推断 golden 具体窗口语义，含/不含边界明确化）
    traits = _infer_golden_calliber_traits(golden_sql)
    time_field = traits.get("time_field")
    asks_time = any(kw in hil_question for kw in ("时间", "当天", "窗口", "自然日", "昨天", "日期", "分区"))
    if time_field and asks_time:
        # 字段名只在 agent 自己提到时才复述（客户不知道数仓字段名）
        field_prefix = f"以 {time_field} 分区字段过滤，" if time_field.lower() in hil_question.lower() else ""
        window_desc = _describe_golden_time_window(golden_sql, time_field)
        if window_desc:
            parts.append(f"时间口径：{field_prefix}{window_desc}")
        else:
            parts.append(
                f"时间口径：{field_prefix}口径与任务描述的时间范围一致"
                "（含任务指定的起止日期，不做额外滚动窗口偏移）。"
            )
    if parts:
        return "".join(parts) + " 请按以上口径继续。"
    return None


def _load_golden_sql_for_reply(matched_task: dict, state: Any, runtime: Any) -> tuple[str, str]:
    """获取 golden SQL 内容与来源路径。

    优先级：
    1. matched_task["golden_sql"]（上游已正确读取的内容，随 task 传递）；
    2. matched_task["golden_sql_path"] 按 _get_task_index_path 解析相对路径重读
       （替代从未被写入的 state["_task_index_path"]）。
    读取失败返回 ("", path)，由调用方退回 LLM 兜底/程序化分支自行处理。
    """
    golden_sql_path = str(matched_task.get("golden_sql_path", "") or "")
    embedded = str(matched_task.get("golden_sql", "") or "").strip()
    if embedded:
        return embedded, golden_sql_path
    if not golden_sql_path:
        return "", ""
    try:
        sql_file = Path(golden_sql_path)
        if not sql_file.is_absolute():
            index_path = str(matched_task.get("_task_index_path", "") or "")
            if not index_path:
                index_path = _get_task_index_path(state, None, runtime) or ""
            if index_path:
                sql_file = Path(index_path).parent / sql_file
        golden_sql = sql_file.read_text(encoding="utf-8").strip()
        if golden_sql:
            return golden_sql, golden_sql_path
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 读取 golden SQL 失败（path={golden_sql_path}）: {e}")
    return "", golden_sql_path


def _generate_and_inject_reply(
    feedback_call: dict,
    messages: list,
    original_query: str,
    matched_task: dict,
    state: Any,
    runtime: Any,
) -> tuple[list, dict] | None:
    """提取 HIL 问题、生成回复并注入 ToolMessage。"""
    hil_question = extract_hil_question(feedback_call)
    if not hil_question:
        logger.debug("[hitl_auto_resolver] 无法提取 HIL 问题内容，跳过")
        return None, None

    # 从 matched_task 获取 golden_sql
    golden_sql, golden_sql_path = _load_golden_sql_for_reply(matched_task, state, runtime)

    # 调用 LLM 生成回复
    answer = _generate_answer(original_query, golden_sql, hil_question, runtime)
    if not answer:
        logger.warning("[hitl_auto_resolver] LLM 生成回复失败，跳过")
        return None, None

    # 注入 ToolMessage
    tool_call_id = feedback_call.get("id", "")
    updated_messages = inject_tool_message(messages, tool_call_id, answer)
    logger.debug("[hitl_auto_resolver] 自动回复已注入")

    # 更新 ActionNode
    update_action_node(state, tool_call_id, answer)

    # 构建 resolved_info
    resolved_info = {
        "question": hil_question,
        "answer": answer,
        "task_name": matched_task.get("name"),
        "task_id": matched_task.get("task_id"),
        "golden_sql_path": golden_sql_path,
    }

    return updated_messages, resolved_info


def _generate_and_inject_reply_v2(
    feedback_call: dict,
    messages: list,
    original_query: str,
    matched_task: dict,
    hil_question: str,
    action: str,
    state: Any,
    runtime: Any,
) -> tuple[list, dict] | None:
    """程序化优先、LLM 兜底，按 action 生成差异化回复并注入 ToolMessage。"""
    golden_sql, golden_sql_path = _load_golden_sql_for_reply(matched_task, state, runtime)

    # 程序化优先（严格 diff / 精确口径），命中则不走 LLM
    programmatic_answer: str | None = None
    # 列枚举死局提问（"期望的 5 列具体包含哪些列"）优先转向——
    # 客户不替 agent 列清单，引导其回到任务原文推导，不再按 action 给答非所问的回复。
    if _is_column_enumeration_ask(hil_question):
        programmatic_answer = (
            "列的具体构成我不能替你枚举——请以任务需求原文为准，逐列推导输出的列数、列名与含义"
            "（每列标注原文出处）。若对聚合粒度、键值对字段格式或排序口径有疑问，"
            "整理成具体选项再来确认。"
        )
    elif action == "TABLE_CONFIRM":
        programmatic_answer = _answer_table_confirm_programmatic(golden_sql, hil_question, original_query, messages)
    elif action == "OUTPUT_SCHEMA_PREVIEW":
        programmatic_answer = _answer_output_schema_programmatic(golden_sql, hil_question)
    elif action == "AMBIGUITY_CLARIFY":
        programmatic_answer = _answer_ambiguity_clarify_programmatic(golden_sql, hil_question)

    if programmatic_answer:
        answer = programmatic_answer
        logger.info(
            f"[hitl_auto_resolver] action={action} 程序化回答命中（不走 LLM）: {answer[:120]!r}"
        )
        tool_call_id = feedback_call.get("id", "")
        updated_messages = inject_tool_message(messages, tool_call_id, answer)
        update_action_node(state, tool_call_id, answer)
        resolved_info = {
            "question": hil_question,
            "answer": answer,
            "task_name": matched_task.get("name"),
            "task_id": matched_task.get("task_id"),
            "golden_sql_path": golden_sql_path,
            "action": action,
            "reply_mode": "programmatic",
        }
        return updated_messages, resolved_info

    # 按 action 选择 prompt 和提取内容（LLM 兜底）
    if action == "TABLE_CONFIRM":
        golden_tables = _extract_golden_tables_from_sql(golden_sql)
        prompt = _ACTION_PROMPTS["TABLE_CONFIRM"].format(
            original_query=original_query[:500],
            golden_tables=golden_tables or "(无法从 golden SQL 提取源表)",
            agent_hil_question=hil_question[:500],
        )
    elif action == "OUTPUT_SCHEMA_PREVIEW":
        schema_summary = _extract_golden_schema_summary(golden_sql)
        prompt = _ACTION_PROMPTS["OUTPUT_SCHEMA_PREVIEW"].format(
            original_query=original_query[:500],
            golden_schema_summary=schema_summary or "(无法从 golden SQL 提取输出结构)",
            agent_hil_question=hil_question[:500],
        )
    elif action == "AMBIGUITY_CLARIFY":
        sql_snippet = _extract_relevant_sql_snippet(golden_sql, hil_question)
        prompt = _ACTION_PROMPTS["AMBIGUITY_CLARIFY"].format(
            original_query=original_query[:500],
            sql_snippet=sql_snippet or "(无法定位相关片段)",
            agent_hil_question=hil_question[:500],
        )
    else:
        # 兜底用原 prompt
        prompt = _HITL_AUTO_RESOLVER_PROMPT.format(
            original_query=original_query,
            golden_sql=golden_sql,
            agent_hil_question=hil_question,
        )

    # 调用 LLM 生成回复
    answer = _generate_answer_from_prompt(prompt, runtime)
    if not answer:
        logger.warning(f"[hitl_auto_resolver] LLM 生成回复失败（action={action}），转兜底回复")
        return None

    # 防泄漏护栏：LLM 回复中出现 agent 未提到的 golden 表名 → 视为答案泄漏，
    # TABLE_CONFIRM 整体替换为客户口吻固定模板；其他 action 剥除泄漏表名。
    answer = _sanitize_llm_reply_leak(answer, golden_sql, hil_question, action)
    # 假确认防线：LLM 顺着 agent 确认了与 golden 实质冲突的选项时改写
    answer = _vet_llm_confirmation(answer, golden_sql, hil_question, action, original_query)

    # 注入 ToolMessage
    tool_call_id = feedback_call.get("id", "")
    updated_messages = inject_tool_message(messages, tool_call_id, answer)
    logger.info(f"[hitl_auto_resolver] action={action} 回复已注入: {answer[:100]!r}")

    # 更新 ActionNode
    update_action_node(state, tool_call_id, answer)

    # 构建 resolved_info
    resolved_info = {
        "question": hil_question,
        "answer": answer,
        "task_name": matched_task.get("name"),
        "task_id": matched_task.get("task_id"),
        "golden_sql_path": golden_sql_path,
        "action": action,
        "reply_mode": "llm",
    }

    return updated_messages, resolved_info


def _get_task_index_path(
    state: Any,
    original_state: Any,
    runtime: Any,
) -> str | None:
    """获取 JSON 索引文件路径。"""
    source = original_state if original_state is not None else state
    if source.get("hitl_task_index_path"):
        path = str(source.get("hitl_task_index_path"))
        logger.debug(f"[hitl_auto_resolver] 从 state 读取 hitl_task_index_path: {path}")
        return path

    if runtime is None:
        logger.warning("[hitl_auto_resolver] runtime 为 None，无法读取配置")
        return None
    get_all_config = getattr(runtime, "get_all_config", None)
    if not callable(get_all_config):
        logger.warning("[hitl_auto_resolver] runtime.get_all_config 不可调用")
        return None
    try:
        config = get_all_config()
        agent_config = config.get("AGENT_CONFIG", {}) if isinstance(config, dict) else {}
        path = agent_config.get("hitl_task_index_path")
        if path:
            logger.debug(f"[hitl_auto_resolver] 从 runtime 配置读取 hitl_task_index_path: {path}")
        else:
            logger.warning(f"[hitl_auto_resolver] runtime 配置中没有 hitl_task_index_path，config={config}")
        return path
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 读取 runtime 配置失败: {e}")
        return None


def _match_golden_sql_by_path(query: str, index_path: str) -> tuple[str | None, dict | None]:
    """通过 JSON 文件匹配 golden SQL（直接读取 golden_sql_path）。

    流程：
    1. 读取 JSON 索引文件（包含 query 和 golden_sql_path 字段）
    2. 根据 query 匹配获取对应的 task
    3. 直接读取 task.golden_sql_path 指定的文件

    匹配策略：综合考虑包含匹配和关键词匹配

    返回：(golden_sql, matched_task)
    """
    index_file = _resolve_index_file(index_path)
    if index_file is None:
        return None, None

    tasks = _load_tasks_from_index(index_file)
    if tasks is None:
        return None, None

    best_match, best_score = _find_best_task_match(query, tasks)
    if not best_match or best_score == 0:
        logger.warning(f"[hitl_auto_resolver] 未找到匹配的 task，query={query[:80]!r}")
        return None, None

    logger.debug(f"[hitl_auto_resolver] 匹配到 task: name={best_match.get('name')}, score={best_score}")
    return _read_golden_sql(best_match, index_file)


def _resolve_index_file(index_path: str) -> Path | None:
    """解析并验证 JSON 索引文件路径。"""
    index_file = Path(index_path)
    logger.debug(f"[hitl_auto_resolver] 检查 JSON 文件: {index_file} (绝对路径: {index_file.resolve()})")
    if index_file.is_file():
        return index_file

    logger.warning(f"[hitl_auto_resolver] JSON 文件不存在: {index_file}，尝试相对于 cwd: {Path.cwd()}")
    alt_path = Path.cwd() / index_path
    if alt_path.is_file():
        logger.debug(f"[hitl_auto_resolver] 使用相对路径文件: {alt_path}")
        return alt_path

    logger.warning(f"[hitl_auto_resolver] 相对路径文件也不存在: {alt_path}")
    return None


def _load_tasks_from_index(index_file: Path) -> list[dict] | None:
    """从 JSON 索引文件加载 tasks。"""
    try:
        import json as _json

        with open(index_file, encoding="utf-8") as f:
            tasks = _json.load(f)
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 读取 JSON 文件失败: {e}")
        return None

    if not isinstance(tasks, list):
        logger.warning(f"[hitl_auto_resolver] JSON 格式错误，期望 list，实际 {type(tasks).__name__}")
        return None

    logger.debug(f"[hitl_auto_resolver] JSON 文件加载成功，共 {len(tasks)} 个 task")
    return tasks


def _calculate_match_score(query_lower: str, task_query: str) -> float:
    """计算 query 与 task_query 的匹配得分。"""
    if task_query == query_lower:
        return 100000  # 精确匹配

    if query_lower in task_query:
        # query 完全包含在 task_query 中
        score = 80 + len(query_lower) / len(task_query) * 10
        logger.debug(
            f"[hitl_auto_resolver] query-in-task 子串匹配, query_len={len(query_lower)}, task_len={len(task_query)}"
        )
        return score

    if task_query in query_lower:
        # task_query 完全包含在 query 中
        score = 70 + len(task_query) / len(query_lower) * 10
        logger.debug(
            f"[hitl_auto_resolver] task-in-query 子串匹配, query_len={len(query_lower)}, task_len={len(task_query)}"
        )
        return score

    # 关键词片段匹配
    query_segments = re.findall(r"[\u4e00-\u9fff]{4,}|[a-z0-9]{4,}", query_lower)
    score = 0
    for seg in query_segments:
        if seg in task_query:
            score += 10
        else:
            for j in range(len(seg) - 3):
                if seg[j : j + 4] in task_query:
                    score += 5  # 部分匹配给较低分数
                    break
    return score


def _find_best_task_match(query: str, tasks: list[dict]) -> tuple[dict | None, float]:
    """在 tasks 中查找与 query 最佳匹配的任务。返回 (best_match, best_score)。"""
    import time

    start_time = time.time()
    query_lower = query.lower()
    logger.debug(f"[hitl_auto_resolver] 原始 query (repr): {repr(query)}, (lower repr): {repr(query_lower)}")

    best_match: dict | None = None
    best_score = 0

    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue

        task_query = str(task.get("query", "")).lower()
        task_name = task.get("name", "N/A")
        golden_sql_path = task.get("golden_sql_path")

        if i < 3:
            logger.debug(f"[hitl_auto_resolver] task[{i}] ({task_name}) query (lower repr): {repr(task_query[:80])}")

        if not task_query or not golden_sql_path:
            continue

        score = _calculate_match_score(query_lower, task_query)

        if score > 0:
            logger.debug(f"[hitl_auto_resolver] task[{i}] ({task_name}) 得分: score={score}")

        if score > best_score:
            best_score = score
            best_match = task

        if score == 100000:  # 精确匹配：找到后直接结束
            logger.debug(f"[hitl_auto_resolver] task[{i}] 精确匹配: name={task.get('name')}, 立即结束匹配")
            break

        if time.time() - start_time > 5:  # 超时保护
            logger.warning(f"[hitl_auto_resolver] 匹配超时，已处理 {i + 1}/{len(tasks)} 个 task")
            break

    logger.debug(f"[hitl_auto_resolver] 匹配完成，耗时: {time.time() - start_time:.2f}s，最佳分数: {best_score}")
    return best_match, best_score


def _read_golden_sql(task: dict, index_file: Path) -> tuple[str, dict]:
    """读取 task 中 golden_sql_path 指定的 SQL 文件内容。"""
    golden_sql_path = task.get("golden_sql_path", "")
    logger.debug(f"[hitl_auto_resolver] 获取到 golden_sql_path: {golden_sql_path}")

    if not golden_sql_path:
        logger.warning("[hitl_auto_resolver] golden_sql_path 为空")
        return None, None  # type: ignore[return-value]

    sql_file = Path(golden_sql_path)
    logger.debug(f"[hitl_auto_resolver] 解析 golden_sql_path: {sql_file}, 是否绝对路径: {sql_file.is_absolute()}")

    if not sql_file.is_absolute():
        sql_file = index_file.parent / sql_file  # 相对于 JSON 文件所在目录
        logger.debug(f"[hitl_auto_resolver] 转换为绝对路径: {sql_file}")

    if not sql_file.is_file():
        logger.warning(f"[hitl_auto_resolver] golden SQL 文件不存在: {sql_file}")
        return None, None  # type: ignore[return-value]

    try:
        golden_sql = sql_file.read_text(encoding="utf-8").strip()
        logger.debug(f"[hitl_auto_resolver] 成功读取 golden SQL，长度: {len(golden_sql)} 字符")
        return golden_sql, task
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 读取 golden SQL 失败: {e}")
        return None, None  # type: ignore[return-value]


def _find_common_substrings(s1: str, s2: str, min_len: int = 4) -> list[str]:
    """找出两个字符串中共同的有意义子串（用于中文匹配）。"""
    common = []
    # 简单滑动窗口匹配
    for length in range(min_len, min(len(s1), len(s2)) + 1):
        for i in range(len(s1) - length + 1):
            substr = s1[i : i + length]
            if substr in s2 and substr not in common:
                common.append(substr)
    return common[:5]  # 最多返回5个


def _generate_answer(
    original_query: str,
    golden_sql: str,
    hil_question: str,
    runtime: Any,
) -> str | None:
    """调用 LLM 生成回复。"""
    if runtime is None:
        logger.warning("[hitl_auto_resolver] runtime 为 None，无法生成回复")
        return None

    llm_getter = getattr(runtime, "llm", None)
    if not callable(llm_getter):
        logger.warning("[hitl_auto_resolver] runtime.llm 不可调用")
        return None

    try:
        llm = llm_getter("planner")
        logger.debug("[hitl_auto_resolver] LLM 获取成功，准备生成回复")
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 获取 planner LLM 失败: {e}")
        return None

    prompt = _HITL_AUTO_RESOLVER_PROMPT.format(
        original_query=original_query,
        golden_sql=golden_sql,
        agent_hil_question=hil_question,
    )

    try:
        logger.debug(f"[hitl_auto_resolver] 调用 LLM，hil_question={hil_question[:100]!r}...")
        response = llm.invoke([SystemMessage(content=prompt), HumanMessage(content="请回答。")])
        content = getattr(response, "content", None)
        if content:
            answer_str = str(content).strip()
            logger.debug(f"[hitl_auto_resolver] LLM 生成回复成功，长度={len(answer_str)}")
            logger.debug(f"[hitl_auto_resolver] LLM 回复内容: {answer_str!r}")
            return answer_str
        return None
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] LLM 调用失败: {e}")
        return None


def _generate_answer_from_prompt(prompt: str, runtime: Any) -> str | None:
    """直接用预构建的 prompt 调用 LLM 生成回复。"""
    if runtime is None:
        logger.warning("[hitl_auto_resolver] runtime 为 None，无法生成回复")
        return None

    llm_getter = getattr(runtime, "llm", None)
    if not callable(llm_getter):
        logger.warning("[hitl_auto_resolver] runtime.llm 不可调用")
        return None

    try:
        llm = llm_getter("planner")
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] 获取 planner LLM 失败: {e}")
        return None

    try:
        response = llm.invoke([SystemMessage(content=prompt), HumanMessage(content="请回答。")])
        content = getattr(response, "content", None)
        if content:
            return str(content).strip()
        return None
    except Exception as e:
        logger.warning(f"[hitl_auto_resolver] LLM 调用失败: {e}")
        return None
