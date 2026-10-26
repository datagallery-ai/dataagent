"""Fixed deliverable checks ported from verify.py, without its query/IR/LLM gate.

Use source tokens for dialect spellings (SQLGlot normalizes TEXT/TRY_CAST) and
AST nodes for DDL declarations. Comments and string values cannot act as code.
CTE references remain owned by QueryChecks' scope-aware implementation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sqlglot import exp

# Each check points back to the old implementation, rather than pretending that
# every item in the old LLM review prompt has become an executable rule.
DELIVERABLE_RULES = {
    "DDL_START": ("_validate_ddl_comprehensive", "DDL 文件必须以 CREATE 开头，前面不能有空白或注释。"),
    "TARGET_DATABASE": ("_validate_target_table_with_database", "目标表必须使用库名.表名。"),
    "DDL_TYPE": (
        "_validate_field_types",
        "普通字段允许 STRING/TINYINT/SMALLINT/INT/BIGINT/DECIMAL(p,s)，与 ddl_rules.md 一致。",
    ),
    "DDL_COMMENT": ("_validate_field_comments", "每个普通字段必须有 COMMENT。"),
    "DDL_TABLE_COMMENT": ("_validate_ddl_comprehensive", "DDL 必须包含表级 COMMENT。"),
    "DDL_NAMING": ("_validate_ddl_naming", "表/字段名仅用小写字母、数字、下划线；表名前缀为 ods/dwd/dws/dim/ads。"),
    "DDL_COMMENT_CHARS": ("_validate_ddl_naming", "字段及表级 COMMENT 禁止包含 *x✖️×@ 字符。"),
    "DDL_PARTITION": (
        "_validate_ddl_comprehensive",
        "分区列必须是 pt_* STRING，注释含“分区”，且不能重复声明为普通列。",
    ),
    "DDL_PRIMARY_KEY": ("_validate_ddl_comprehensive", "DDL 禁止 PRIMARY KEY。"),
    "STATIC_PARTITION": ("_validate_insert_overwrite_partition", "INSERT 必须包含 pt_* 分区的静态赋值。"),
    "LATERAL_VIEW_JOIN": ("_validate_lateral_view_join", "LATERAL VIEW 后不能直接 JOIN，应先封装为子查询。"),
    "SCHEDULER_SINGLE_LINE": ("_validate_scheduler_variable_single_line", "调度变量 ${...} 必须位于同一行。"),
    "UNSUPPORTED_FUNCTION": ("_validate_unsupported_functions", "当前 Spark 3.1 环境禁止 TRY_CAST/ARRAY_AGG。"),
    "UNIX_TO_STR": ("_validate_no_unix_to_str", "UNIX_TO_STR 应使用 from_unixtime 替代。"),
    "REGEXP_SPLIT": ("_validate_no_regexp_split", "REGEXP_SPLIT 应使用 SPLIT 替代。"),
    "CAST_TEXT": ("_validate_no_text_in_cast", "CAST 的 TEXT 类型应使用 STRING 替代。"),
    "ARRAY_UNIQUE_AGG": ("_validate_no_array_unique_agg", "ARRAY_UNIQUE_AGG 应使用 COLLECT_SET 替代。"),
    "QUALIFY": ("_validate_no_qualify", "当前 Spark 3.1 环境不支持 QUALIFY 子句。"),
    "COLLECT_LIST_ORDER": ("_validate_no_concat_ws_collect_list_order_by", "COLLECT_LIST 参数不支持 ORDER BY。"),
    "UDF_PREFIX": (
        "_validate_dateformat_prefix/_validate_concat_with_rank_udf",
        "平台 UDF 必须使用 bicoredata. 前缀。",
    ),
    "JOIN_TYPE": ("_validate_join_type_explicit", "JOIN 必须显式指定类型。"),
}


class DeliverableChecks:
    """Check the old fixed SQL delivery requirements against the current candidate."""

    def __init__(self, sql: str, kind: str, statement: exp.Expression, tokens: list[Any], emit: Callable) -> None:
        self.sql, self.kind, self.statement, self.tokens, self.emit = sql, kind, statement, tokens, emit
        self.issues: list[Any] = []
        code = [" "] * len(sql)
        for token in tokens:
            if token.token_type.name not in {"STRING", "IDENTIFIER"}:
                code[token.start : token.end + 1] = sql[token.start : token.end + 1]
        self.masked = "".join(code)

    def detect(self) -> list[Any]:
        """Return only program findings; this module never calls a model or reads IR."""
        target = self.statement.this
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table) and not target.db:
            self._add("TARGET_DATABASE", node=target.this)
        if self.kind == "ddl":
            self._ddl(target)
        else:
            self._dml()
        return self.issues

    def _add(
        self,
        rule: str,
        node: exp.Expression | None = None,
        span: tuple[int, int] | None = None,
        replacement: str | None = None,
        detail: str = "",
    ) -> None:
        start, end = span or (0, len(self.sql))
        if node is not None and "start" in node.meta:
            start, end = node.meta.get("start"), node.meta.get("end") + 1
        self.issues.append(self.emit(rule, "engineering", self.kind, self.sql, start, end, replacement, detail))

    def _ddl(self, target: exp.Expression) -> None:
        if not re.match(r"CREATE\b", self.sql, re.I):
            self._add("DDL_START")
        if not re.fullmatch(r"[a-z0-9_]+", target.name) or not target.name.startswith(
            ("ods", "dwd", "dws", "dim", "ads")
        ):
            self._add("DDL_NAMING", node=target.this)
        columns = [col for col in self.statement.this.expressions if isinstance(col, exp.ColumnDef)]
        partition = self.statement.find(exp.PartitionedByProperty)
        partitions = partition.this.expressions if partition is not None else []
        ordinary_names = {col.name.lower() for col in columns}
        for col in columns + list(partitions):
            if not isinstance(col, exp.ColumnDef):
                self._add("DDL_PARTITION")
                continue
            if not re.fullmatch(r"[a-z0-9_]+", col.name):
                self._add("DDL_NAMING", node=col.this)
            comment = col.find(exp.CommentColumnConstraint)
            dtype = col.args.get("kind")
            # Read the spelling after the column identifier: TEXT and STRING
            # share an AST type, but only STRING is in this delivery whitelist.
            after = col.this.meta.get("end", -1) + 1
            type_token = next((token for token in self.tokens if token.start >= after), None)
            spelling = self.sql[type_token.start : type_token.end + 1].upper() if type_token else ""
            if col in partitions:
                if (
                    not col.name.startswith("pt_")
                    or spelling != "STRING"
                    or col.name.lower() in ordinary_names
                    or comment is None
                    or "分区" not in comment.this.name
                ):
                    self._add("DDL_PARTITION", node=col.this)
            else:
                allowed = spelling in {"STRING", "TINYINT", "SMALLINT", "INT", "BIGINT", "DECIMAL"}
                if not allowed or (spelling == "DECIMAL" and len(dtype.expressions) != 2):
                    self._add("DDL_TYPE", node=col.this, detail=f"字段 {col.name}: {spelling}。")
                if comment is None:
                    self._add("DDL_COMMENT", node=col.this)
            if comment is not None:
                self._comment(comment.this)
        table_comment = self.statement.find(exp.SchemaCommentProperty)
        if table_comment is None:
            self._add("DDL_TABLE_COMMENT")
        else:
            self._comment(table_comment.this)
        for match in re.finditer(r"\bPRIMARY\s+KEY\b", self.masked, re.I):
            self._add("DDL_PRIMARY_KEY", span=match.span())

    def _comment(self, literal: exp.Expression) -> None:
        if re.search(r"[*x✖️×@]", literal.name):
            self._add("DDL_COMMENT_CHARS", node=literal)

    def _dml(self) -> None:
        partition = self.statement.args.get("partition") or self.statement.this.args.get("partition")
        entries = partition.expressions if partition is not None else []
        if not any(isinstance(item, exp.EQ) and item.left.name.startswith("pt_") for item in entries):
            self._add("STATIC_PARTITION")
        functions = {
            "TRY_CAST": ("UNSUPPORTED_FUNCTION", None),
            "ARRAY_AGG": ("UNSUPPORTED_FUNCTION", None),
            "UNIX_TO_STR": ("UNIX_TO_STR", "from_unixtime"),
            "REGEXP_SPLIT": ("REGEXP_SPLIT", "SPLIT"),
            "ARRAY_UNIQUE_AGG": ("ARRAY_UNIQUE_AGG", "COLLECT_SET"),
        }
        for name, (rule, replacement) in functions.items():
            for match in re.finditer(rf"\b({name})\s*\(", self.masked, re.I):
                before = self.masked[: match.start()].rstrip()
                # A schema-qualified custom UDF may share a built-in spelling.
                if before.endswith("."):
                    continue
                self._add(rule, span=match.span(1), replacement=replacement, detail=f"{name}()。")
        for match in re.finditer(r"\b(DateFormat|ConcatWithRank)\s*\(", self.masked, re.I):
            before = self.masked[: match.start()]
            if not re.search(r"\bbicoredata\s*\.\s*$", before, re.I):
                # An explicitly different namespace needs human evidence.
                patch = None if before.rstrip().endswith(".") else "bicoredata." + match.group(1)
                self._add("UDF_PREFIX", span=match.span(1), replacement=patch)
        for token in self.tokens:
            if token.token_type.name == "STRING":
                fragment = self.sql[token.start : token.end + 1]
                for match in re.finditer(r"\$\{[^}]*[\r\n][^}]*\}", fragment):
                    span = (token.start + match.start(), token.start + match.end())
                    self._add("SCHEDULER_SINGLE_LINE", span=span, replacement=re.sub(r"\s+", " ", match.group()))
        self._token_structure()
        for select in self.statement.find_all(exp.Select):
            if select.args.get("qualify") is not None:
                self._add("QUALIFY")
        for join in self.statement.find_all(exp.Join):
            if not join.args.get("kind") and not join.args.get("side") and not join.args.get("method"):
                self._add("JOIN_TYPE", detail=join.sql(dialect="spark"))

    def _token_structure(self) -> None:
        depth, lateral_depths, functions = 0, set(), []
        previous = ""
        for token in self.tokens:
            kind, text = token.token_type.name, token.text.upper()
            if kind in {"STRING", "IDENTIFIER"}:
                previous = ""
                continue
            if kind == "L_PAREN":
                depth += 1
                functions.append((depth, previous))
            elif kind == "R_PAREN":
                lateral_depths.discard(depth)
                functions = [(level, name) for level, name in functions if level < depth]
                depth -= 1
            elif kind == "SELECT":
                lateral_depths.discard(depth)
            elif kind == "VIEW" and previous == "LATERAL":
                lateral_depths.add(depth)
            elif kind == "JOIN" and depth in lateral_depths:
                self._add("LATERAL_VIEW_JOIN", span=(token.start, token.end + 1))
            elif text == "ORDER BY" and any(name == "COLLECT_LIST" for _, name in functions):
                self._add("COLLECT_LIST_ORDER", span=(token.start, token.end + 1))
            elif text == "TEXT" and previous == "AS" and any(name == "CAST" for _, name in functions):
                self._add("CAST_TEXT", span=(token.start, token.end + 1), replacement="STRING")
            previous = text if kind not in {"STRING", "IDENTIFIER"} else ""
