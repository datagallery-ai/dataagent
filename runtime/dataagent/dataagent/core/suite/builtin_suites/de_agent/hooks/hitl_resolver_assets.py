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

# v46 口径缺口问型标记（query 缺口分析六模式的提问措辞）：这些业务口径问题
# （序列去重 / 指标产生机制 / 名称映射字典 / 业务词取值 / 明细还是聚合取用层级）
# 无关键词命中时会被 default 判成 UNA 拒答——agent 按纪律 13 提问却拿不到答复，
# 白耗交互预算。仅对 base=UNA 的问题做改判（_caliber_gap_override），已分类为
# TABLE_CONFIRM/OUTPUT/AMBIGUITY 的问题不受影响（回放门 182 问零回归的前提：
# 历史仅 3 问为 UNA 且不含以下标记，实测验证）。
_CALIBER_GAP_ASK_MARKERS: tuple[str, ...] = (
    "去重",        # 序列键值对是否去重（"去重口径"已走 AMBIGUITY 关键词，此条兜底其余措辞）
    "产生机制",     # 预估/评分类指标的产生机制
    "计算机制",     # 同上变体
    "对照字典",     # 代码→中文名对照字典来源
    "映射字典",     # 同上变体
    "名称映射",     # 同上变体
    "对应取值",     # 业务词在本表的对应取值（枚举映射）
    "取值映射",     # 同上变体
    "明细还是",     # 原始明细还是聚合表取用层级
    "取还是",       # 同上变体（"从明细表取还是从聚合表取"——"明细还是"被"表取"隔开的措辞）
    "还是聚合",     # 同上变体（"用明细还是聚合表"）
    "取用层级",     # 同上名词化措辞
    "二次加工",     # 引用已有聚合表二次加工
)

# IR→HITL 提问标记（format_ir_hitl_pending_action 固定前缀/字段标题）：
# 用于 hitl_auto_resolver 把 DataTaskIR 队列提问路由到可代答 action，避免落 UNA 拒答。
_IR_HITL_MARKERS: tuple[str, ...] = (
    "DataTaskIR",
    "未确认口径",
)
_IR_HITL_AMBIGUITY_TITLES: tuple[str, ...] = (
    "【特征主键】",
    "【时间上下文】",
    "【输出特征目录】",
)
_IR_HITL_SOURCE_TITLES: tuple[str, ...] = (
    "【数据源与关联预处理】",
    "【数据源",
)

# 追问式改判信号（_mixed_question_override 的口径信号扩展专用）：裸"去重"在
# 历史语料以陈述形出现（q01"维度去重两个口径问题"、q67"取最新一条去重后
# 聚合序列"），直接用会把 2 个历史 TABLE_CONFIRM 问改判（回放门 50/47/82/3
# 破防）；追问语境只认疑问形。其余标记历史零碰撞，原样复用。
_DEDUP_ASK_RE = re.compile(r"是否.{0,8}去重|去重吗|要不要去重|需要去重|去重还是保留|保留还是去重")
_CALIBER_GAP_FOLLOWUP_MARKERS = tuple(m for m in _CALIBER_GAP_ASK_MARKERS if m != "去重")

# v47 列构成确认标记（_schema_derivation_una_override 专用）：agent 推导出列构成
# 方案后向用户确认的问型。v46_test13q q47 Q4 实证：此类问型含"输出schema"（无
# 空格）与"列构成"——OUTPUT 关键词表是"输出 schema"（带空格）与"列结构"，
# 双双近失配落 UNA 被拒答，恰好挡住最需要"期望输出 N 列"确认的问型。
# 仅对 base=UNA 改判（历史回放 3 UNA 问不含以下标记，回放门验证）。
_SCHEMA_DERIVATION_MARKERS: tuple[str, ...] = (
    "列构成",
    "列方案",
    "输出schema",  # 无空格变体（"输出 schema"带空格已走 OUTPUT 关键词）
)

# v47 收敛确认标记（_table_proposal_una_override 专用）：检索上限后向用户收敛
# 确认最接近候选表的问型（v46_test13q q55 Q3 铁例："候选池中最接近的表…请您
# 确认是否接受"——"候选池"与 TABLE_CONFIRM 关键词"候选表"子串差一字不匹配，
# 零命中落 UNA 白耗一轮）。刻意窄于 _TABLE_PROPOSAL_RE：后者含"我倾向"等宽
# 信号，会把 q64 型"我倾向于理解为…请确认这个理解是否正确"的口径歧义问
# 误改判（回放门实证 50→51），收敛确认的签名=表为确认对象。
_TABLE_CONVERGENCE_MARKERS: tuple[str, ...] = (
    "最接近的表",
    "候选池",
    "最终候选",
    "作为源表",
    "作为数据源",
    "作为主数据源",
    "唯一匹配",
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

# v48 确认请求形剥离（_tables_are_confirmed_references 专用）："请您确认/需要您确认/
# 请问您确认使用"是确认请求（表名/方案为待确认提案），子串上却含"您确认/确认使用"
# 引用标记——极性反转会让新表提案被误判为已确认引用（v47_smoke q01 铁例：近变体表
# 提案问被 _mixed_question_override 改判 AMBIGUITY，表提案无人应答，错误兄弟表锁定）。
# 先剥离请求形再查引用标记；真引用形（"按您确认的源表""已确认使用 X"）不带请求
# 前缀，不受影响。前缀集合=语料+冒烟实测措辞（请/请问/麻烦/烦请/需要/要/希望）。
# v53b 补"用户"变体：v48 只剥"您"，v52_r2 q34 实测 agent 以第三人称谈用户
# （"需要用户确认使用哪个作为主源"）——"用户"二字挡住剥离，残余"确认使用"再度
# 极性反转，真表提案（含我倾向选择 db.table 全套提案信号）被改判 AMBIGUITY、
# 表判定无人应答（错误近变体宽表锁定 + 后续口径答复被 agent 误读为默认同意）。
# 真"用户已确认"引用形带"已"字不匹配请求前缀+确认，不受影响。
_REQUEST_CONFIRM_FORM_RE = re.compile(r"(?:请问|请|麻烦|烦请|需要|要|希望)(?:您|用户)?确认(?:使用)?")

# 口径决策提问信号：决策对象是口径/取数方式/标识选择（"请选择取数口径""需确认两个口径"），
# 表名仅作上下文出现（"源表 X 是每日快照""源表已内含1年窗口"）。
# v3.5 实测（q61/q42）：此类提问被 TABLE_CONFIRM 模板答非所问，agent 拿不到口径答复后独自定错口径。
_CALIBER_DECISION_RE = re.compile(r"口径|取哪[一]?天|是否包含当天|用哪个字段|按什么维度")

# 新表候选提案表述：表名是待选对象（"计划使用 X""唯一匹配的表""作为源表"）。
# 含此类表述时即使带口径关键词也保持 TABLE_CONFIRM（候选表未定，阻塞点在选表）。
# v48 收窄：原裸"候选/我倾向/你倾向"三支撞非表候选措辞——"候选"撞标识字段候选
# 枚举（q59"三个候选：方案A：device_id"/q49"没有其他候选"实证：源表已定仅问
# 字段，被裸"候选"拦住改判、TABLE_CONFIRM 模板答非所问），"我倾向/你倾向"撞
# 口径理解确认（q64"我倾向于理解为"）。"候选"改为结构感知：其后 4 字符内接
# "表"字或 db.table 名才是表候选（"候选 biads.x_dm""候选源表""候选1（单表）"），
# "候选字段/候选：方案A"不命中。表选择语境词（数据源(表)选择/候选数据源/
# 数据源候选/源表确认/计划使用/唯一匹配/作为源表）整词承载。仅 _mixed_question_override
# 的口径改判防护消费本 RE（_proposes_table_candidates）；纯表提案问（无口径信号）
# 不经此路径，不受收窄影响。回放语料实测：恰 5 翻转全为质量修正（q02/q34/q70
# AMB→TC 表提案修正，q49/q59 TC→AMB 标识选择修正），见 replay_check.py 头注释。
_TABLE_PROPOSAL_RE = re.compile(
    r"计划使用|唯一匹配|选哪个表|选表|数据源表|源表确认|确认源表|数据源选择|"
    r"源表选择|候选数据源|数据源候选|数据来源|作为.{0,12}(?:源表|主事实表|核心源表|事实表)|"
    r"候选.{0,4}(?:表|[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)"
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

# L1 近变体口径判别（v42）：表名 token → 客户口吻口径词。被拒表与 golden 表
# token 差集经此表转译（golden 侧独有 = 我要的口径，被拒侧独有 = 不要的口径），
# 注入 TABLE_CONFIRM 拒绝/升级答复。与层级/粒度标签同源零泄漏边界：只译
# token 为业务口径词，完整表名/db/字段仍禁。token Jaccard ≥ 1/3 才判近变体，
# 完全无关的被拒表不给口径提示（防误伤）；双向差集都无映射时回落现行为。
_VARIANT_TOKEN_CONCEPTS = {
    "event": "事件流水（埋点事件明细）",
    "billing": "计费流水（计费系统口径）",
    "ocpc": "oCPC（按点击计费优化）",
    "ocpd": "oCPD（按下载计费优化）",
    "uba": "行为分析（UBA）",
    "ubr": "卸载备份记录（UBR）",
    "noassist": "无辅助转化（noassist）",
    "sample": "抽样数据",
    "detail": "明细数据",
    "stat": "统计汇总数据",
    "statistics": "统计汇总数据",
    "list": "列表清单数据",
    "feature": "特征表（预计算）",
    "fulllist": "全量列表",
    # v44 扩充（2026-10-20 复盘：15/55 拒绝轮因差集 token 无映射空手——
    # q47 型 qa/result/search、q58 型 unified/client/install、q34 型 instance/cvr、
    # q55 型 xtr、q37 型 order/pay）
    "qa": "问答（QA）口径",
    "result": "结果类数据（处理后产出）",
    "search": "搜索行为口径",
    "query": "搜索词（query）口径",
    "app": "应用（APP）维度",
    "client": "客户端行为口径",
    "install": "安装行为口径",
    "uninstall": "卸载行为口径",
    "unified": "统一行为（多来源合并）",
    "instance": "实例级（单次投放/转化实例）",
    "cvr": "转化率（CVR）口径",
    "pctr": "预估点击率（pCTR）口径",
    "pcvr": "预估转化率（pCVR）口径",
    "xtr": "预估目标排序（xTR）口径",
    "order": "订单口径",
    "pay": "付费/支付口径",
    "use": "使用行为口径",
    "open": "打开行为口径",
    "down": "下载行为口径",
    "download": "下载行为口径",
    "nlu": "NLU 语义分析口径",
    "nlp": "NLP 自然语言处理口径",
    "tf": "事务流水（transaction flow 明细）",
    "bhv": "行为（behavior）明细",
    "log": "日志流水",
    "hm": "小时级",
    "dm": "天级",
    "v2": "第二版口径",
    "1y": "近1年窗口",
    "30d": "近30天窗口",
    "23d": "近23天窗口",
    "14d": "近14天窗口",
    "7d": "近7天窗口",
    "3d": "近3天窗口",
    "1d": "当日/单日窗口",
    "1m": "近1月窗口",
    "3m": "近3月窗口",
}


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
