"""SQL 选择器 Prompt 模板 — Top-K 一次性裁决

- 一次调用即把全部 Top-K 候选交给 LLM 裁决，不再两两比较后汇总胜负矩阵
- prompt 内显式解释 consistency（一致性得分）的来源与含义，引导 LLM 把它当作软先验
- 每个候选附带 SQL 与执行结果预览（前 5 行 + 末行）
- 输出为单个 JSON 对象 {"reasoning": ..., "choice": <候选序号>}，便于结构化解析
"""

TOP_K_SELECTION_PROMPT = """You are auditing several candidate SQL queries written for one natural-language question. Exactly one candidate answers the question best; pick it.

# Where the consistency value comes from
Every candidate was run against the database and then grouped by its result set. The "consistency" attached to a candidate is the share of all executed candidates that returned the very same result set as this one. A higher value means more independently generated queries agreed on that answer, so treat it as a soft prior rather than a verdict: a crowded answer can still be wrong, and a rare one can still be right.

# How to decide
- Study the question, the hint and the database schema before looking at the candidates.
- For each candidate, check whether both its SQL logic and the rows it returned truly satisfy the question.
- Return only the columns the question asks for; punish candidates that add extra columns or drop required ones.
- When the evidence is balanced, prefer the candidate with the higher consistency; override that preference only when another candidate is clearly more faithful to the question.

# Response format
Reply with one single JSON object and nothing else:
{{
  "reasoning": "Go through the candidates one by one, say why each does or does not fit the question, then justify your final pick.",
  "choice": <integer index of the chosen candidate>
}}
The "choice" value MUST be one of the candidate indices listed below. Emit no text outside the JSON object and do not wrap it in code fences.

# Database schema
{DATABASE_SCHEMA}

# Question
{QUESTION}

# Hint
{HINT}

# Candidates
{CANDIDATES_BLOCK}
"""


def format_topk_selection_prompt(
    database_schema: str,
    question: str,
    hint: str,
    candidates_block: str,
) -> str:
    """格式化 Top-K 一次性选择 Prompt

    Args:
        database_schema: 数据库 schema 描述
        question: 用户问题
        hint: 提示/证据
        candidates_block: 已拼装好的候选清单（含序号、consistency、SQL、执行结果预览）

    Returns:
        格式化后的 prompt 字符串
    """
    return TOP_K_SELECTION_PROMPT.format(
        DATABASE_SCHEMA=database_schema,
        QUESTION=question,
        HINT=hint or "None",
        CANDIDATES_BLOCK=candidates_block,
    )
