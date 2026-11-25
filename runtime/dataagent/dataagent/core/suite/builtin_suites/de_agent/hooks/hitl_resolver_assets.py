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
"""de_agent suite HITL auto-resolver 的纯数据资产（prompt/关键词表/口径语义表），场景常量与逻辑解耦。

改关键词表/口径语义/prompt 会改变问答路由行为，需过回放回归门
（182 问分布 TABLE_CONFIRM 51 / AMBIGUITY_CLARIFY 46 / OUTPUT_SCHEMA_PREVIEW 82 / UNA 3）。

本模块经包路径 import；与 hook 文件的单文件直载通道（suite-root 隔离加载）
无关，同样禁止模块级 mutable 状态（纯常量与推导表除外）。
"""

from __future__ import annotations

import re

from dataagent.core.flex.hooks.hitl_classifier import HitlActionSpec
# 代答口头约束：可补充提问未写明但正确交付必需的业务信息；除非 Agent 提问已出现，
# 否则不主动抛表名/英文字段/SQL 原文（贴近真人客户口吻）。
_HITL_NL_REPLY_RULES = """
【回答口吻 · 必须遵守】
1. 用自然语言业务话回答，像真人客户：说「按账号维度」「只要指定那一个小时」「还要排除无效应用」。
2. 除非 Agent 提问里已经写出，否则回复中禁止主动出现：表名（含 db.table）、英文字段名
   （如 user_id/pt_d/app_id）、SQL 关键字或片段（SELECT/WHERE/GROUP BY/PARTITION/INSERT/
   collect_list 等）、以及参考材料中的原文照搬。
3. 参考口径里有、但提问未覆盖、又对结果正确必要的信息——可以补充，必须先译成业务自然语言，
   禁止把参考材料里的英文标识或 SQL 原文直接挪进回复。
4. 仅当 Agent 提问已给出候选表名/字段名时，才可从中点名选用（复述提问用词）；不要替 Agent
   发明它没提过的英文名。
""".strip()

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

""" + _HITL_NL_REPLY_RULES + """

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

""" + _HITL_NL_REPLY_RULES + """

角色约束：输出形态（列数、聚合粒度、一行代表什么）是你的需求，可以直接要求；
但禁止逐条报出具体列名清单让 Agent 照抄。回答聚焦：列数是否一致、
按什么粒度聚合（一行代表什么）、是否明细宽表/汇总表等结构层面差异。""",
    "AMBIGUITY_CLARIFY": """你是任务提出方（客户），正在回答 Agent 的口径澄清问题。

## 你的业务需求
{original_query}

## 参考口径（仅供你内部判断，禁止在回复中复述原文/英文字段/SQL）

## 参考口径（仅供你内部判断，禁止在回复中复述）
{sql_snippet}

## Agent 的提问
{agent_hil_question}

""" + _HITL_NL_REPLY_RULES + """
 
角色约束：你只知道自己的业务口径。回答用业务话（如「按设备维度统计」「时间窗口含当天」）；
Agent 给出了候选字段时可以从中选定（复述提问用词即可）。简洁回答。""",
    # Pre-SQL DataTaskIR 最终检视：对照 golden 决定确认或补充遗漏口径（与 AMBIGUITY 同属口径类）。
    "IR_CALIBER_REVIEW": """你是任务提出方（客户）。Agent 在生成 SQL 前请你核对【已确认口径摘要】是否完整、正确。
默认偏保守纠错：有冲突先补充/纠正，不要图省事直接确认。
 
## 你的业务需求
{original_query}
 
## 参考口径（仅供你内部判断，禁止逐条复述 SQL / 表名 / 英文字段原文）
{sql_snippet}
 
## Agent 的提问（含已确认口径摘要与核对点）
{agent_hil_question}
 
""" + _HITL_NL_REPLY_RULES + """
 
对照参考口径与摘要（粒度、输出形态、列数、时间/过滤边界、去重排序）。
有冲突先用自然语言指出要改成什么；确认没有时才回复「确认，按此生成」。简洁作答。""",
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
# 分类行为等价的前提）；UNA 无关键词，作 default_action。
HITL_DEFAULT_ACTION = "UNA"
HITL_ACTION_SPECS: tuple[HitlActionSpec, ...] = tuple(
    HitlActionSpec(name=name, keywords=tuple(keywords))
    for name, keywords in HITL_ACTION_KEYWORDS.items()
)

# 口径缺口问型标记（query 缺口分析六模式的提问措辞）：这些业务口径问题
# （序列去重 / 指标产生机制 / 名称映射字典 / 业务词取值 / 明细还是聚合取用层级）
# 无关键词命中时会被 default 判成 UNA 拒答——agent 按纪律 13 提问却拿不到答复，
# 白耗交互预算。仅对 base=UNA 的问题做改判（_caliber_gap_override），
# 已分类为 TABLE_CONFIRM/OUTPUT/AMBIGUITY 的问题不受影响。
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

# IR→HITL 提问标记：reason 仍带 DataTaskIR/未确认口径；pending 用业务标题。
# 用于 hitl_auto_resolver 把 DataTaskIR 队列提问路由到可代答 action，避免落 UNA 拒答。
_IR_HITL_MARKERS: tuple[str, ...] = (
    "DataTaskIR",
    "未确认口径",
    "待确认业务口径",
)
_IR_HITL_AMBIGUITY_TITLES: tuple[str, ...] = (
    "【统计主键】",
    "【时间范围】",
    "【输出指标】",
    # legacy titles (older pending / logs)
    "【特征主键】",
    "【时间上下文】",
    "【输出特征目录】",
)
_IR_HITL_SOURCE_TITLES: tuple[str, ...] = (
    "【数据来源与关联】",
    "【数据源与关联预处理】",
    "【数据源",
)
# Pre-SQL caliber review（format_ir_caliber_review_pending 固定前缀）。
_IR_CALIBER_REVIEW_MARKERS: tuple[str, ...] = (
    "口径最终检视",
)

# 追问式改判信号（_mixed_question_override 的口径信号扩展专用）：裸"去重"常
# 以陈述形出现（如"维度去重两个口径问题""取最新一条去重后聚合序列"），
# 直接用会把陈述形 TABLE_CONFIRM 问改判；追问语境只认疑问形。
_DEDUP_ASK_RE = re.compile(r"是否.{0,8}去重|去重吗|要不要去重|需要去重|去重还是保留|保留还是去重")
_CALIBER_GAP_FOLLOWUP_MARKERS = tuple(m for m in _CALIBER_GAP_ASK_MARKERS if m != "去重")

# 语义/业务含义问标记（多部件应答合成消费）：复合提问中"某个后缀/命名/拆分
# 维度的业务含义"型子问（解读A/候选B 选项式或开放式）。此类子问无表名、无
# 列数信号，程序化应答器（表判定/列数对比/口径原语）全部覆盖不到——若不
# 单独识别，整问被主 action 程序化应答部分覆盖后该子问永久无答复。
# 刻意不含"方案A/候选A"等裸选项标签（表选择问同样使用），只认含义类提问措辞。
_SEMANTIC_ASK_MARKERS: tuple[str, ...] = (
    "业务含义",
    "含义",
    "区分维度",
    "什么意思",
    "如何理解",
    "指的是什么",
    "代表什么",
)

# 复合提问的节头签名：结构化 agent 提问用 markdown 节承载多个待确认项
# （"## 待确认1：…"/"### _p1 表…"）。≥2 个节头时按节切分部件（节承载完整
# 子问，表候选不会被句读打散）；无节头回退子句切分。
_MULTIPART_HEADER_RE = re.compile(r"(?m)^(?:#{2,3}\s*|\*\*(?:待确认|确认事项))")

# 部件问句形态：只有承载"待回答之问"的部件才参与多部件应答——描述性段落
# （"该表记录的是xx含义的数据""已确认口径回述"）与背景铺垫不含问句形态，
# 误应答会产出复述噪音（回述段被当输出列问再答一遍）。
_PART_ASK_FORM_RE = re.compile(r"待确认|请确认|需确认|需要确认|是否|还是|哪个|哪些|什么|怎么|吗|[?？]")

# 多部件应答的缺口补答 prompt（_compose_multipart_answer 消费）：
# 主 action 程序化应答覆盖不到的语义子问集中一次 LLM 补答。golden SQL 仅作
# 内部判定素材（哪个解读/选项符合任务真实意图），泄漏纪律与主 LLM 兜底一致
# （_sanitize_llm_reply_leak + _vet_llm_confirmation 照常在补答段上运行）。
_GAP_TOPUP_PROMPT = """你是任务提出方（客户），正在回答 Agent 一次提问中包含的多个子问题。
其中关于数据源表选择、输出列数、统计口径的部分已由系统答复，剩下这些子问题需要你答复：

{gap_questions}

## 任务原始需求
{original_query}

## 参考判定素材（仅供你内部判断，禁止在回复中透露其存在或内容）
{golden_sql}

要求：
1. 只回答上面列出的子问题，不要重复数据源表、输出列数、统计口径相关内容。
2. 若参考素材能判断哪个解读/选项符合任务真实意图：以客户口吻明确选定该选项，
   用业务语言说明理由（不用 SQL 术语）。
3. 若无法判断：不要迎合 Agent 的倾向选项（禁止顺着倾向假确认），说明这属于
   Agent 应基于任务原文与已确认数据源自行推导的内容，请其对照任务原文和
   已确认表的字段说明再推导。
4. 回复中不得出现 Agent 未在其提问中提到的表名/字段名。
5. 简洁，直接给结论。"""

# 列构成确认标记（_schema_derivation_una_override 专用）：agent 推导出列构成
# 方案后向用户确认的问型。此类问型常含"输出schema"（无空格）与"列构成"——
# 与 OUTPUT 关键词表的"输出 schema"（带空格）/"列结构"近失配落 UNA 被拒答，
# 恰好挡住最需要"期望输出 N 列"确认的问型。仅对 base=UNA 改判。
_SCHEMA_DERIVATION_MARKERS: tuple[str, ...] = (
    "列构成",
    "列方案",
    "输出schema",  # 无空格变体（"输出 schema"带空格已走 OUTPUT 关键词）
)

# 收敛确认标记（_table_proposal_una_override 专用）：检索上限后向用户收敛
# 确认最接近候选表的问型（"候选池中最接近的表…请您确认是否接受"——
# "候选池"与 TABLE_CONFIRM 关键词"候选表"子串差一字不匹配，零命中落 UNA
# 白耗一轮）。刻意窄于 _TABLE_PROPOSAL_RE：后者含"我倾向"等宽信号，会把
# "我倾向于理解为…请确认这个理解是否正确"的口径歧义问误改判；
# 收敛确认的签名=表为确认对象。
_TABLE_CONVERGENCE_MARKERS: tuple[str, ...] = (
    "最接近的表",
    "候选池",
    "最终候选",
    "作为源表",
    "作为数据源",
    "作为主数据源",
    "唯一匹配",
)



_HITL_AUTO_RESOLVER_PROMPT = """你是任务提出方（客户）。用户正在等待对以下问题的回复。

## 用户的原始需求
{original_query}

## 内部参考材料（仅供判断；禁止在回复中原文照搬表名/英文字段/SQL）
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

# 引用标记表域限定（_tables_are_confirmed_references 专用，双信号）：
# ①标记 ±10 字符窗内含表语境词（表/数据源/方案/候选）或 db.table 限定名；
# ②标记所在分隔子句（。！？；;，,\n 切分）内含完整 db.table 限定名——覆盖括注形
# "…ads_x_stat_dm**（您已确认符合需求）"：表名与标记同句但相距 40+ 字符，±10
# 窗扫不到，子句级限定名信号保其分类不回归。
# 反例形："(3)用户已确认 user_id 标识和单日分区快照口径。需确认源表选择…"——
# "已确认"落在标识口径上：窗内无表词（"目标表"距标记 13 字符在窗外），
# 子句内无 db.table 限定名（user_id 裸标识无点；前句表名为 bare 名无点；
# pending 提案表在另一段落）→ 不计引用。否则该问新表提案（方案A/B/C 三候选）
# 被误判已确认引用、改判 AMBIGUITY、表判定无人应答，agent 沿用已被拒的表
# 直至交付。
# 真引用形（"源表已确认为 biads.t1"/"用户确认使用方案A了"/"数据源已定
# agapdw.dwd_y_dm"）标记天然邻接表词/限定名，不受影响。
_TABLE_SCOPE_CONTEXT_RE = re.compile(r"表|数据源|方案|候选")
_QUALIFIED_TABLE_NAME_RE = re.compile(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", re.IGNORECASE)
_CLAUSE_DELIM_CHARS = "。！？；;，,\n"

# 确认请求形剥离（_tables_are_confirmed_references 专用）："请您确认/需要您确认/
# 请问您确认使用"是确认请求（表名/方案为待确认提案），子串上却含"您确认/确认使用"
# 引用标记——极性反转会让新表提案被误判为已确认引用（表提案问被
# _mixed_question_override 改判 AMBIGUITY，表提案无人应答，错误兄弟表锁定）。
# 先剥离请求形再查引用标记；真引用形（"按您确认的源表""已确认使用 X"）不带请求
# 前缀，不受影响。前缀集合=实测措辞（请/请问/麻烦/烦请/需/需要/要/希望）。
# 中间可带"您/用户"（agent 会以第三人称谈用户："需要用户确认使用哪个作为主源"，
# 也会省"要"字写"需您确认选哪一套"/"需确认使用哪张作为主源"——裸"需"缩略形
# 单字不在早期前缀集合，剥离漏过后残余"您确认/确认使用"极性反转，表提案问被
# 改判 AMBIGUITY、表判定无人应答）。
# 裸"需"仅后接（您|用户）?确认时剥离；真"用户已确认"引用形带"已"字不匹配请求
# 前缀+确认、真引用形"按您确认的/已确认使用"的"确认"前是"您已/已"等非前缀字，
# 均不受影响。"无需确认"否定形同被剥离——其本身非引用标记载体，剥离后无
# "已确认/您确认"残留，行为不变。
_REQUEST_CONFIRM_FORM_RE = re.compile(r"(?:请问|请|麻烦|烦请|需|需要|要|希望)(?:您|用户)?确认(?:使用)?")

# 口径决策提问信号：决策对象是口径/取数方式/标识选择（"请选择取数口径""需确认两个口径"），
# 表名仅作上下文出现（"源表 X 是每日快照""源表已内含1年窗口"）。
# 此类提问若被 TABLE_CONFIRM 模板答非所问，agent 拿不到口径答复后会独自定错口径。
_CALIBER_DECISION_RE = re.compile(r"口径|取哪[一]?天|是否包含当天|用哪个字段|按什么维度")

# 新表候选提案表述：表名是待选对象（"计划使用 X""唯一匹配的表""作为源表"）。
# 含此类表述时即使带口径关键词也保持 TABLE_CONFIRM（候选表未定，阻塞点在选表）。
# 注意收窄：裸"候选"会撞标识字段候选枚举（源表已定仅问字段时被拦住改判、
# TABLE_CONFIRM 模板答非所问），"我倾向/你倾向"会撞口径理解确认（"我倾向于
# 理解为"）。故"候选"为结构感知：其后 4 字符内接"表"字或 db.table 名才是
# 表候选（"候选 biads.x_dm""候选源表""候选1（单表）"），"候选字段/候选：方案A"
# 不命中。表选择语境词（数据源(表)选择/候选数据源/数据源候选/源表确认/计划使用/
# 唯一匹配/作为源表）整词承载。仅 _mixed_question_override 的口径改判防护消费
# 本 RE（_proposes_table_candidates）；纯表提案问（无口径信号）不经此路径，
# 不受收窄影响。
_TABLE_PROPOSAL_RE = re.compile(
    r"计划使用|唯一匹配|选哪个表|选表|数据源表|源表确认|确认源表|数据源选择|"
    r"源表选择|候选数据源|数据源候选|数据来源|作为.{0,12}(?:源表|主事实表|核心源表|事实表)|"
    r"候选.{0,4}(?:表|[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)|"
    # "<角色>表选 db.table" 提案形（"安装行为表选 biads.x""应用维表选
    # agapdw.y"）——表选后 4 字符内接 db.table 全名才是提案（"总表选哪个
    # 字段"的"表选"后是疑问词，不命中）；带口径词的表选型问（"口径一致性，
    # 需您确认选哪一套"）不再被改判 AMBIGUITY、表判定无人应答。真引用形由
    # _tables_are_confirmed_references 分支先行拦截，不受本支影响。
    r"表选.{0,4}[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*"
)


# 表选择问型签名（hitl_auto_resolver._table_selection_output_override 消费）：
# 源表/数据源 12 字符窗内的选用动词，或"方案A…还是"对比形，或"选用/改用/
# 选择…还是"形。与"≥2 个不同表候选"联用才构成签名——纯输出结构问（列数/
# 列名/目标表确认）不携带源表选用动词，不受影响。回放语料 82 个 OUTPUT 问
# 零命中本签名（含 ≥2 表的 14 个 OUTPUT 问均无选用动词，已仿真验证）。
_TABLE_SELECTION_ASK_RE = re.compile(
    r"(?:源表|数据源)[^。？\n]{0,12}(?:选用|改用|选择|用哪)"
    r"|方案\s*[AB12][^。？\n]{0,30}还是"
    r"|(?:选用|改用|选择)[^。？\n]{0,60}还是"
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

# L1 近变体口径判别：表名 token → 客户口吻口径词。被拒表与 golden 表
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
    # 常见差集 token 扩充（qa/result/search、unified/client/install、
    # instance/cvr、xtr、order/pay 等）
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
# 确认动词被否定（"不建议用/不要采用/无需确认"）才豁免防线审查——若子句里出现
# 裸"不/没/非"字就整句免审，会被表变体描述词误触发：如 OUTPUT 分支回复
# "确认选用 X（不截断版本）"——"不"属"不截断"描述词（该变体的卖点），并非
# 否定确认本身，整个错误表确认子句免审漏网，错误变体被口头锁定。
# 语义：否定词 + 可选情态字 + 确认动词词根（用/使/采/确/同/选/定/按/建/推/符
# 对应 用/使用/采用/确认/同意/选/定/按/建议/推荐/符合）才判"确认被否定"。
# "不截断/不含/非空/不一定"等描述词性否定不豁免——此类子句若真在确认错误
# 表，防线照常改写。裸"非"（非常/非空高频撞词）与"排除"（"排除X后，确认Y"
# 的Y分句按逗号切分后独立成句，照常审查）不整句豁免。
_NEGATED_CONFIRM_RE = re.compile(
    r"(?:不|别|勿|莫|无需|不必|没有|并非|禁止?)"
    r"(?:要|能|可|得|应|宜|需|再)?"
    r"(?:用|使|采|确|同|选|定|按|建|推|符)"
)
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
