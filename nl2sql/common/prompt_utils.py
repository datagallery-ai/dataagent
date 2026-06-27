"""Generator/Validator 共用的 Prompt 工具函数"""
from typing import List, Dict, Any, Optional


def get_enhanced_database_schema_profile(data_item) -> str:
    """获取数据库 schema profile

    由于 schema_text 已由 schema_linking 模块格式化，直接透传。
    """
    return data_item.database_schema_after_schema_linking


_GUIDANCE_HEADER = "\n".join([
    "## Historical SQL Patterns and Insights:",
    "",
    "These patterns are summarized from past NL2SQL cases spanning multiple databases.",
    "[CRITICAL] Treat them as user-preferred, alternative approaches for reference only "
    "— not as absolute correctness rules.",
    "",
])

_GUIDANCE_FOOTER = "\n".join([
    "[REMINDER] The cases above come from other database schemas; "
    "reuse the reasoning rather than copying the SQL verbatim.",
    "",
])


def _render_pattern_block(label: str, sql: str, strategy: Dict[str, Any], note: str) -> List[str]:
    """渲染单个 SQL 模式块（不推荐 / 推荐）；缺 sql 或 strategy 时返回空。"""
    if not (sql and strategy):
        return []
    block = [f"**{label}:**", "```example sql", sql, "```"]
    if strategy.get("pattern"):
        block.append(f"  - Pattern: `{strategy['pattern']}`")
    if strategy.get("implication"):
        block.append(f"  - {note}: {strategy['implication']}")
    block.append("")
    return block


def _render_guidance_case(idx: int, guidance: Dict[str, Any]) -> List[str]:
    """渲染单条历史案例：意图、不推荐/推荐模式、可执行建议。"""
    case = [f"### Case {idx}: {guidance.get('intent', 'Unknown intent')}", ""]
    case += _render_pattern_block(
        "Less Preferred Pattern",
        guidance.get("qualified_incorrect_sql", ""),
        guidance.get("strategy_incorrect", {}),
        "Issue",
    )
    case += _render_pattern_block(
        "Preferred Pattern",
        guidance.get("qualified_correct_sql", ""),
        guidance.get("strategy_correct", {}),
        "Benefit",
    )
    advice = guidance.get("actionable_advice", "")
    if advice:
        case += [f"**Actionable Advice:** {advice}", ""]
    case += ["---", ""]
    return case


def format_sql_guidance(sql_guidance_items: Optional[List[Dict[str, Any]]]) -> str:
    """将历史案例的 SQL guidance 条目格式化为可拼接进 prompt 的文本。"""
    if not sql_guidance_items:
        return ""

    sections: List[str] = [_GUIDANCE_HEADER]
    for idx, guidance in enumerate(sql_guidance_items, 1):
        sections.extend(_render_guidance_case(idx, guidance))
    sections.append(_GUIDANCE_FOOTER)
    return "\n".join(sections)


def get_sql_guidance(data_item) -> str:
    """获取 SQL guidance"""
    if hasattr(data_item, 'sql_guidance_items') and data_item.sql_guidance_items:
        return format_sql_guidance(data_item.sql_guidance_items)
    return ""
