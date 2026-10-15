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
"""de_agent suite HITL auto-resolver 的纯数据资产（prompt/关键词表/口径语义表）。

从原 flex hooks 目录下的 hitl_auto_resolver hook 原样迁出的场景常量，与逻辑解耦：
改关键词表/口径语义/prompt 需过 ``v36_campaign/replay_check.py`` 回放门
（182 问分布 TABLE_CONFIRM 50 / AMBIGUITY 47 / OUTPUT 82 / UNA 3）。

本模块经包路径 import；与 hook 文件的单文件直载通道（suite-root 隔离加载）
无关，同样禁止模块级 mutable 状态（纯常量与推导表除外）。
"""

from __future__ import annotations

import re

from dataagent.core.flex.hooks.hitl_classifier import HitlActionSpec

# ── 4 个 action 的差异化 prompt ─────────────────────────────────────────
# TABLE_CONFIRM: 数据源表选择确认（拦数据源错）
# OUTPUT_SCHEMA_PREVIEW: 输出 schema 预览（拦输出结构错）
# AMBIGUITY_CLARIFY: 关键歧义澄清（拦标识混用/时间窗口错）
# UNA: 不当问题（直接索要 golden / 无意义反问）→ 拒绝，让 agent 自主决策
_ACTION_PROMPTS = {
    "TABLE_CONFIRM": """你是任务提出方（客户），正在回答 Agent 关于数据源选择的确认请求。

## 你的业务需求
{original_query}

## Agent 的提问
{agent_hil_question}

## 参考判定（仅供你内部判断，禁止在回复中出现）
{golden_tables}

你的角色约束（必须遵守）：
1. 你只知道自己的业务需求，不知道数仓里有哪些表、表名、字段名。
2. 回复中禁止出现 Agent 提问中未提到的表名/字段名——你不能替 Agent 做选表。
3. 若 Agent 给出了候选选项，你从中选择符合需求的一项/多项并说明业务理由；
   若都不符合，说明为什么不符合（从需求角度），并要求 Agent 再给 2-3 个候选选项。
4. 若 Agent 没给选项，要求 Agent 基于元数据检索后给出候选选项（含表的用途说明），你来选。""",
    "OUTPUT_SCHEMA_PREVIEW": """你是任务提出方（客户），正在确认 Agent 描述的输出结构。

## 你的业务需求
{original_query}

## 你期望的输出形态（仅供内部判断，禁止逐条复述列名清单）
{golden_schema_summary}

## Agent 描述的输出
{agent_hil_question}

角色约束：输出形态（列数、聚合粒度、一行代表什么）是你的需求，可以直接要求；
但禁止逐条报出具体列名清单让 Agent 照抄。回答聚焦：列数是否一致、
按什么粒度聚合（一行代表什么）、是否明细宽表/汇总表等结构层面差异。""",
    "AMBIGUITY_CLARIFY": """你是任务提出方（客户），正在回答 Agent 的口径澄清问题。

## 你的业务需求
{original_query}

## 参考口径（仅供你内部判断，禁止在回复中复述）
{sql_snippet}

## Agent 的提问
{agent_hil_question}

角色约束：你只知道自己的业务口径，不知道数仓字段名。回答只能基于业务口径
（如"按设备维度统计""时间窗口含当天"）；Agent 给出了候选字段时可以从中选定，
但禁止主动引入 Agent 未提到的字段名/表名。简洁回答。""",
    "UNA": (
        "这个问题我无法直接确认。若你已有倾向方案，请把具体方案和 1-2 个候选"
        "（含表名/字段名/时间窗口/列数等具体值）列出来再问我，我来帮你确认；"
        "否则请基于元数据和题意自主判断，继续执行，不要重复询问相同问题。"
    ),
}

# Action 关键词映射（按优先级：TABLE_CONFIRM > OUTPUT_SCHEMA_PREVIEW > AMBIGUITY_CLARIFY > UNA）
HITL_ACTION_KEYWORDS = {
    # Final 阶段（输出结构确认）优先级最高：输出相关问题常含"表名"指目标表，不能被 TABLE_CONFIRM 抢走
    "OUTPUT_SCHEMA_PREVIEW": ["输出 schema", "列结构", "列数", "输出字段", "输出列", "字段顺序", "列名", "目标表名", "输出表", "输出结构", "输出格式", "最终交付", "最终输出", "是否符合预期"],
    # Initial 阶段（源表确认）：源表/数据源关键词，注意"目标表名"已归 OUTPUT，不会冲突
    "TABLE_CONFIRM": ["源表", "数据源", "候选表", "选哪个表", "table selection", "源表选择", "数据来源", "方案A", "方案B", "方案C"],
    # Mid 阶段（关键歧义澄清）
    "AMBIGUITY_CLARIFY": ["用户标识", "device_id", "user_id", "oaid", "imei", "时间窗口", "是否包含当天", "时间范围", "过滤条件", "主键", "JOIN key", "去重口径", "目标粒度", "GROUP BY", "分区策略"],
}

# 分类器注册表与缺省类别：顺序即关键词匹配优先级，必须与 HITL_ACTION_KEYWORDS
# 的定义序一致（OUTPUT_SCHEMA_PREVIEW → TABLE_CONFIRM → AMBIGUITY_CLARIFY，
# 回放门 182 问分布的行为等价前提）；UNA 无关键词，作 default_action。
HITL_DEFAULT_ACTION = "UNA"
HITL_ACTION_SPECS: tuple[HitlActionSpec, ...] = tuple(
    HitlActionSpec(name=name, keywords=tuple(keywords))
    for name, keywords in HITL_ACTION_KEYWORDS.items()
)


_HITL_AUTO_RESOLVER_PROMPT = """你是 SQL 领域的专家。用户正在等待对以下问题的回复。

## 用户的原始需求
{original_query}

## 该场景的参考 golden SQL
{golden_sql}

## Agent 向你请教的问题
{agent_hil_question}

请结合用户的原始需求和参考 golden SQL，用简洁、自然的语言回答 Agent 的问题。你要根据golden SQL的逻辑和细节回复 Agent 的问题，但回答中一定
不要直接出现golden SQL的内容，也不要向 Agent 透露你拥有 golden SQL。
回复应该帮助 Agent 理解用户意图和实现思路，从而顺利完成这个任务。
直接简单直接给出回答即可，不需要额外的格式。"""


# 已确认引用标记：Mid/Final 阶段追问常礼貌引用先前已确认的源表（"源表已确认为 X，
# 现在确认标识/时间口径"）。这类表名是上下文引用而非新候选提案，不能据此判 TABLE_CONFIRM。
_CONFIRMED_REFERENCE_MARKERS = ("已确认", "已定", "确认使用", "您确认", "上一步已", "先前已", "定下来")

# 口径决策提问信号：决策对象是口径/取数方式/标识选择（"请选择取数口径""需确认两个口径"），
# 表名仅作上下文出现（"源表 X 是每日快照""源表已内含1年窗口"）。
# v3.5 实测（q61/q42）：此类提问被 TABLE_CONFIRM 模板答非所问，agent 拿不到口径答复后独自定错口径。
_CALIBER_DECISION_RE = re.compile(r"口径|取哪[一]?天|是否包含当天|用哪个字段|按什么维度")

# 新表候选提案表述：表名是待选对象（"候选""我倾向选择 X 作为源表""唯一匹配的表"）。
# 含此类表述时即使带口径关键词也保持 TABLE_CONFIRM（候选表未定，阻塞点在选表）。
_TABLE_PROPOSAL_RE = re.compile(
    r"候选|我倾向|你倾向|计划使用|唯一匹配|选哪个表|数据源表|源表确认|确认源表|数据源选择|"
    r"源表选择|作为.{0,12}(?:源表|主事实表|核心源表|事实表)|数据来源|选表"
)


# 标识字段候选（sql_feature_eval 场景跨题共性：标识体系混用是高频失败模式）
_IDENTIFIER_FIELD_CANDIDATES = (
    "device_id2", "device_id", "user_id", "oaid", "imei", "uid", "uuid",
    "up_id", "open_uid", "huawei_id", "aid", "adid",
)

# 字段名 → 客户口吻业务概念。零泄漏边界：业务概念描述允许，golden 表名/字段名
# 禁止——resolver 扮演任务提出方，只用自己的业务词汇描述需求粒度，字段名的
# schema linking 留给 agent 自己完成。
_FIELD_CONCEPT_MAP = {
    "user_id": "账号ID",
    "uid": "账号原始ID",
    "up_id": "账号统一ID",
    "open_uid": "开放账号UID",
    "huawei_id": "华为账号ID",
    "device_id": "设备ID",
    "device_id2": "设备ID（二代）",
    "did": "设备ID",
    "dsid": "设备ID",
    "uuid": "设备唯一标识UUID",
    "oaid": "广告标识OAID",
    "imei": "设备硬件标识IMEI",
    "aid": "应用ID",
    "adid": "广告ID",
    "app_id": "应用ID",
    "appid": "应用ID",
    "site_id": "榜单ID",
    "siteid": "榜单ID",
    "list_id": "榜单ID",
    "listid": "榜单ID",
    "task_id": "任务ID",
    "taskid": "任务ID",
    "search_words": "搜索词",
    "query": "搜索词",
    "org_query": "原始搜索词",
    "app_type": "应用类型",
    "expid": "实验ID",
    "pricepolicy": "转化目标",
    "tail": "设备尾号",
    "pt_h": "小时",
}


# 变量名 → (业务口吻标签, 相对基准日的天偏移；非天级粒度用 None)
_TIME_VAR_SEMANTICS = {
    "date": ("任务基准日（当天）", 0),
    "date_ep": ("任务基准日（当天）", 0),
    "date_1": ("任务基准日（当天）", 0),
    "date_2": ("任务基准日（当天）", 0),
    "last_date": ("任务基准日（当天）", 0),
    "last_seven_day": ("基准日往前7天那天", -7),
    "last_three_day": ("基准日往前3天那天", -3),
    "threeMonthsAgo": ("3个月前那天", None),
    "last_month_ep": ("上月对应日", None),
    "start_time": ("任务指定的起始日", None),
}

_TIME_RHS_TOKEN = r"(?:['\"]\$\{[^}]+\}['\"]|['\"]\$\w+['\"]|\$\{[^}]+\}|\$\w+)"


# golden 表名前缀/后缀 → 数仓层级/时间粒度特征（检索-拒绝死局升级指引的素材；
# 只描述特征不报表名，零泄漏边界内）
_TABLE_LAYER_PREFIXES = (
    ("ods", "原始贴源层（几乎未加工的原始数据）"),
    ("dwd", "明细层（按主题清洗的明细数据）"),
    ("dws", "汇总层（按主题预聚合的数据）"),
    ("ads", "应用层（面向应用/报表的成品数据）"),
    ("dim", "维表（维度描述信息）"),
)
_TABLE_SUFFIX_GRAINS = (
    ("_dm", "天级"),
    ("_hm", "小时级"),
)


_CLAUSE_DELIM_RE = re.compile(r"([。！？；;，,\n])")
_CLAUSE_QUESTION_RE = re.compile(r"[?？]|还是|哪个|哪些|是否|吗")
_CLAUSE_NEGATION_RE = re.compile(r"不|没|非|排除|并非")
_CONFIRM_DIRECTIVE_RE = re.compile(r"用|使用|采用|确认|同意|选|定|按|以|符合我的需求")
_TABLE_PRONOUN_RE = re.compile(r"该表|此表|这张表|这些表|该数据源|这个数据源|上述表")

# 标识体系字段 → 概念（含无下划线变体，如 deviceid）。只 vet 标识体系，
# join key/维度字段（app_id/site_id 等）不在范围，避免误伤正确的关联说明子句。
_VET_IDENTIFIER_FIELD_CONCEPT_BASE = {
    "device_id": "设备ID",
    "device_id2": "设备ID（二代）",
    "did": "设备ID",
    "dsid": "设备ID",
    "uuid": "设备唯一标识UUID",
    "user_id": "账号ID",
    "uid": "账号原始ID",
    "up_id": "账号统一ID",
    "open_uid": "开放账号UID",
    "huawei_id": "华为账号ID",
    "oaid": "广告标识OAID",
    "imei": "设备硬件标识IMEI",
    "aid": "应用ID",
    "adid": "广告ID",
}
_VET_IDENTIFIER_FIELD_CONCEPT = {
    **_VET_IDENTIFIER_FIELD_CONCEPT_BASE,
    **{f.replace("_", ""): c for f, c in _VET_IDENTIFIER_FIELD_CONCEPT_BASE.items()},
}

# 中文概念词 → 概念（覆盖"用设备ID"式无英文字段名的确认）
_VET_ZH_CONCEPT_KEYWORDS = (
    ("设备id", "设备ID"), ("设备标识", "设备ID"), ("设备维度", "设备ID"), ("按设备", "设备ID"),
    ("账号id", "账号ID"), ("用户id", "账号ID"), ("账号维度", "账号ID"), ("用户维度", "账号ID"),
    ("按用户", "账号ID"), ("账号标识", "账号ID"),
    ("广告标识", "广告标识OAID"),
    ("硬件标识", "设备硬件标识IMEI"), ("串号", "设备硬件标识IMEI"),
)


# agent 要求用户枚举"期望哪些列/字段"的死局提问模式。
# 客户不会替 agent 列清单（列构成应从任务原文推导），命中即给转向答复。
_COLUMN_ENUMERATION_ASK_PATTERN = re.compile(
    r"(期望|需要|想要|请|明确).{0,12}(哪些列|哪\s*\d+\s*列|列清单|包含哪些字段|哪些字段|具体包含哪些|具体是哪些)"
)
