import re

DEFAULT_EXCLUDE_FIELDS = frozenset(
    {
        "pt_d",
        "pt",
        "pt_h",
        "create_time",
        "update_time",
        "modify_time",
        "id",
        "uuid",
        "udid",
        "guid",
        "row_key",
    }
)


def _split_top_level_commas(text: str) -> list[str]:
    """按**顶层**逗号切分（括号内、字符串里的逗号不算分隔符）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in text:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return parts


def parse_ddl_column_types(ddl_sql: str) -> dict[str, str]:
    """从 Hive DDL 里解析 ``{列名: 小写类型}``。

    支持 ``CREATE EXTERNAL TABLE ... ( col STRING COMMENT '...' , ... )``，
    忽略 ``PARTITIONED BY`` 里的分区列（分区列不进业务字段）。
    """
    if not ddl_sql:
        return {}
    # 去掉注释，避免注释里的括号干扰列清单切分。
    text = re.sub(r"/\*.*?\*/", " ", ddl_sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)

    open_m = re.search(r"\((?!\s*PARTITIONED\s+BY\b)", text, re.IGNORECASE)
    if not open_m:
        return {}
    # 找到与该左括号配对的右括号。
    depth = 0
    close_idx = -1
    for i in range(open_m.end() - 1, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break
    if close_idx < 0:
        return {}

    body = text[open_m.end() : close_idx]
    column_types: dict[str, str] = {}
    for part in _split_top_level_commas(body):
        part = part.strip()
        if not part:
            continue
        # ``col TYPE [COMMENT '...']`` —— 类型名是第一个 token。
        m = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)`?\s+([A-Za-z_][A-Za-z0-9_]*)", part)
        if not m:
            continue
        column_types[m.group(1)] = m.group(2).lower()
    return column_types
