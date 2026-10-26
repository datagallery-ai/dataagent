"""Deterministic SQL findings with source locations and verified model patches.

These detectors cover explicit engineering rules. They do not decide business
grain, population, NULL retention, deduplication, or ranking intent from prose.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from dataagent.core.suite.builtin_suites.de_agent.tools.sql_checker_static import parse_artifact, table_name
from dataagent.core.suite.builtin_suites.de_agent.tools.sql_deliverable_checks import (
    DELIVERABLE_RULES,
    DeliverableChecks,
)
from dataagent.core.suite.builtin_suites.de_agent.tools.sql_query_checks import QueryChecks

SCOPES = ("engineering", "grain_join", "time", "null_filter", "metrics", "sequence")
COVERAGE = {
    "engineering": "原交付静态检查：DDL 类型/注释/命名、分区、禁用函数/UDF 前缀、JOIN/CTE、列和元数据核对",
    "grain_join": "常量窗口排序、关联键与聚合键不一致风险；不证明任意维表唯一性",
    "time": "宏算术、DateFormat 参数数、日期整数加减、分区边界和元数据时间戳单位冲突",
    "null_filter": "属性过滤先于取最新；不裁决 LEFT JOIN 后过滤、NULL 保留或补值口径",
    "metrics": "整数截断风险、字符串数值比较；不裁决分母兜底和舍入阶段",
    "sequence": "ConcatWithRank 参数数/GROUP BY、排序号来源/缩放、collect_list 顺序风险；不推断 TopN/业务编码",
}


@dataclass(frozen=True)
class Finding:
    """A program-produced finding with an optional repair hint, never a required replacement."""

    issue_id: str
    rule_id: str
    scope: str
    file: str
    start: int
    end: int
    fragment: str
    message: str
    evidence: str
    replacement: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize a finding, including its optional replacement hint."""
        return asdict(self)


class RuleDetector:
    """Scan SQL using tokens, regex and AST without constructing or invoking an LLM."""

    def __init__(self, rules_path: Path, disabled_rules: list[str] | None = None, schema: dict | None = None) -> None:
        """Bind the canonical rule text and explicitly configured exceptions."""
        self.sources = {
            name: (rules_path / name).read_text(encoding="utf-8") for name in ("ddl_rules.md", "dml_rules.md")
        }
        self.document = "\n\n".join(self.sources.values())
        self.rules_hash = hashlib.sha256(self.document.encode()).hexdigest()
        self.disabled_rules = set(disabled_rules or [])
        self.schema = schema or {}

    def detect(self, artifacts: dict[str, str]) -> list[Finding]:
        """Return source-backed issues for one artifact pair without severity levels."""
        findings: list[Finding] = []
        statements = {}
        for kind in ("ddl", "dml"):
            sql = artifacts.get(kind, "")
            try:
                tokens = sqlglot.Dialect.get_or_raise("hive" if kind == "ddl" else "spark").tokenize(sql)
                statements.update({kind: parse_artifact(sql, kind)})
            except (ValueError, sqlglot.errors.SqlglotError):
                findings.append(self._finding("PARSE_UNVERIFIED", "engineering", kind, sql, 0, len(sql)))
                continue
            code = [" "] * len(sql)
            for token in tokens:
                if token.token_type.name not in {"STRING", "IDENTIFIER"}:
                    code[token.start : token.end + 1] = sql[token.start : token.end + 1]
            masked = "".join(code)
            findings.extend(DeliverableChecks(sql, kind, statements.get(kind), tokens, self._finding).detect())
            if kind == "dml":
                findings.extend(self._write_mode(sql, masked, statements.get(kind)))
                findings.extend(self._macros(sql, tokens))
                findings.extend(self._functions(sql, masked, tokens))
                findings.extend(self._window_checks(sql, statements.get(kind)))
                findings.extend(QueryChecks(sql, statements.get(kind), self._finding, self.schema).detect())
        if len(statements) == 2:
            findings.extend(self._artifacts(artifacts, statements))
        return [item for item in findings if item.rule_id not in self.disabled_rules]

    def apply(self, artifacts: dict[str, str], finding: Finding, response: dict[str, Any]) -> dict[str, str]:
        """Apply model-chosen edits to unique original text; preserve statement and target boundaries."""
        if set(response) - {"issue_id", "edits", "reason"} or response.get("issue_id") != finding.issue_id:
            raise ValueError("Repair must name exactly the reported issue")
        original = artifacts.get(finding.file, "")
        if original[finding.start : finding.end] != finding.fragment:
            raise ValueError("Stale source location; run detection again")
        edits = response.get("edits")
        if not isinstance(edits, list) or not edits:
            raise ValueError("No repair edits supplied")
        locations: dict[str, list[tuple[int, int, str]]] = {}
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"file", "old_text", "new_text"}:
                raise ValueError("Each edit must contain file, old_text and new_text")
            kind, old, new = edit.get("file"), edit.get("old_text"), edit.get("new_text")
            if not isinstance(kind, str) or kind not in {"ddl", "dml"}:
                raise ValueError("Edit file must be ddl or dml")
            if not isinstance(old, str) or not old or not isinstance(new, str) or old == new:
                raise ValueError("Each edit needs a nonempty original anchor and changed replacement")
            source = artifacts.get(kind, "")
            start = source.find(old)
            if start < 0 or source.find(old, start + 1) >= 0:
                raise ValueError("Edit old_text must have exactly one unique match in the original SQL")
            locations.setdefault(kind, []).append((start, start + len(old), new))
        result = dict(artifacts)
        for kind, spans in locations.items():
            source = artifacts.get(kind, "")
            boundary, updated = len(source), source
            for start, end, replacement in sorted(spans, reverse=True):
                if end > boundary:
                    raise ValueError("Repair edits overlap in the original SQL")
                updated = updated[:start] + replacement + updated[end:]
                boundary = start
            try:
                before_target = table_name(parse_artifact(source, kind))
                after_target = table_name(parse_artifact(updated, kind))
            except (ValueError, sqlglot.errors.SqlglotError) as exc:
                raise ValueError("Cannot verify a single statement and its target table") from exc
            if after_target != before_target:
                raise ValueError("Repair changed the target table")
            result.update({kind: updated})
        return result

    def verify_repair(self, before: list[Finding], after: list[Finding], issue: Finding) -> None:
        """Require fewer instances of the addressed rule without increasing any other rule count."""
        previous = Counter((item.rule_id, item.file) for item in before)
        current = Counter((item.rule_id, item.file) for item in after)
        key = (issue.rule_id, issue.file)
        if current.get(key, 0) >= previous.get(key, 0):
            raise ValueError("The reported rule still has as many violations; changing its text is not a repair")
        if any(count > previous.get(key, 0) for key, count in current.items()):
            raise ValueError("Repair introduced or increased a rule violation")

    def _evidence(self, marker: str) -> str:
        for name, document in self.sources.items():
            for number, line in enumerate(document.splitlines(), start=1):
                if marker in line:
                    return f"sql_rules/references/{name}:{number}: {line.strip()}"
        raise ValueError(f"Canonical rule is missing: {marker}")

    def _finding(
        self,
        rule: str,
        scope: str,
        kind: str,
        sql: str,
        start: int,
        end: int,
        replacement: str | None = None,
        detail: str = "",
    ) -> Finding:
        definitions = {
            "PARSE_UNVERIFIED": ("SQL 方言配置", "解析器未能确认语句；可能是语法或解析器覆盖限制，不自动猜测修复。"),
            "WRITE_MODE": ("DML 必须使用 INSERT OVERWRITE", "DML 写入方式不符合 INSERT OVERWRITE 规范。"),
            "TARGET_MISMATCH": ("DML 必须使用 INSERT OVERWRITE", "DDL 与 DML 目标表不同，需要明确目标表。"),
            "DDL_COLUMNS": ("DML INSERT OVERWRITE 列序必须与 DDL", "DDL 列缺失或存在重复列。"),
            "PARTITION_MISMATCH": ("DDL中的PARTITIONED BY列集合", "DDL/DML 分区缺失或集合不一致。"),
            "PROJECTION_ALIGNMENT": ("DML INSERT OVERWRITE 列序必须与 DDL", "最终投影列数/顺序无法与 DDL 明确对齐。"),
            "PROJECTION_UNVERIFIED": (
                "Spark 的 INSERT OVERWRITE 按位置匹配列",
                "列数相同但名称不同；不能仅据别名判定位置映射错误。",
            ),
            "DDL_EXTERNAL": ("- DDL 使用 Hive", "DDL 缺少 EXTERNAL 或 IF NOT EXISTS。"),
            "DDL_STORAGE": ("- DDL 使用 Hive", "DDL 存储格式不是 ORC。"),
            "CTE_POSITION": ("WITH/CTE 必须位于 INSERT OVERWRITE 之前", "WITH/CTE 不在 INSERT 之前。"),
            "MACRO_ARITHMETIC": ("宏表达式的偏移量必须是数字常量", "调度宏偏移量包含算术表达式。"),
            "DATEFORMAT_ARITY": ("只能传入日期字符串一个参数", "bicoredata.DateFormat 必须只有一个参数。"),
            "CONCAT_RANK_ARITY": (
                "bicoredata.ConcatWithRank(value, rank_col, separator)",
                "ConcatWithRank 参数数不为 3。",
            ),
            "CONSTANT_WINDOW_ORDER": ("排序字段可能并列时", "窗口排序键都是常量/分组键，无法证明多行的选择依据。"),
            "SEQUENCE_GROUP_BY": ("必须和GROUP BY一起使用", "ConcatWithRank 所在聚合查询缺少 GROUP BY。"),
            "SEQUENCE_SCALED_RANK": (
                "以及类似方式处理类型为小数的排序字段",
                "排序号使用乘倍数转整数；可能丢失精度/溢出。",
            ),
            "SEQUENCE_RANK_ORIGIN": ("先用 `POSEXPLODE` 保留原始位置", "未能追溯排序号到位置或窗口排名，需确认来源。"),
            "SEQUENCE_UNSTABLE_AGG": ("禁止用 `CONCAT_WS", "collect_list 的使用存在顺序/兼容性风险。"),
            "FILTER_BEFORE_LATEST": ("按 JOIN key 用窗口函数取最近", "属性过滤先于选取记录，可能改变保留行。"),
            "PARTITION_WINDOW_BOUNDS": ("要同时指定上界和下界", "分区范围只识别到单侧边界。"),
            "DATE_INTEGER_ARITHMETIC": ("不能将日期 CAST 为数字再直接", "将天分区转整数后加减不能保证日历语义。"),
            "TIMESTAMP_UNIT_MIX": ("unix 秒时间戳与 unix 毫秒时间戳", "操作数的元数据声明了不同时间戳单位。"),
            "INTEGER_TRUNCATION": ("用户要求进行非零过滤时", "整数 CAST 后聚合/非零过滤可能截掉小数。"),
            "STRING_NUMERIC_COMPARISON": (
                "如果列是字符串类型，先转换成数值类型再比较",
                "字符串列直接参与数值大小比较。",
            ),
            "COLUMN_SCOPE": ("定义必须先于引用", "引用了上游显式结果中不存在的字段。"),
            "METADATA_COLUMN_UNKNOWN": ("禁止凭业务语义臆造字段名", "召回的源表字段列表中未找到该列。"),
            "JOIN_KEY_UNIQUENESS": (
                "不能简单 `GROUP BY join_key, attribute`",
                "按关联键加属性聚合不能证明关联键唯一。",
            ),
        }
        marker, message = definitions.get(rule, ("SQL 方言配置", rule))
        if rule in DELIVERABLE_RULES:
            source, message = DELIVERABLE_RULES.get(rule)
            evidence = f"de_agent/tools/verify.py::{source} — {message}"
            if rule == "DDL_TYPE":
                evidence = self._evidence("普通字段类型只使用")
        else:
            evidence = self._evidence(marker)
        fragment = sql[start:end]
        identity = f"{rule}:{kind}:{start}:{fragment}"
        issue_id = hashlib.sha256(identity.encode()).hexdigest()[:16]
        return Finding(
            issue_id,
            rule,
            scope,
            kind,
            start,
            end,
            fragment,
            f"{message} {detail}".strip(),
            evidence,
            replacement,
        )

    def _write_mode(self, sql: str, masked: str, statement: exp.Expression) -> list[Finding]:
        if statement.args.get("overwrite"):
            return []
        match = re.search(r"\bINSERT\s+INTO\b", masked, re.I)
        if match:
            return [self._finding("WRITE_MODE", "engineering", "dml", sql, *match.span(), "INSERT OVERWRITE")]
        return [self._finding("WRITE_MODE", "engineering", "dml", sql, 0, len(sql))]

    def _macros(self, sql: str, tokens: list[Any]) -> list[Finding]:
        findings = []
        # Quoted macro values are intentional SQL data; comments are absent from tokens.
        pattern = re.compile(r"\$\{start_time,\s*([+\-\d* /]+)\s*,[^{}]+\}")
        for token in tokens:
            if token.token_type.name != "STRING":
                continue
            fragment = sql[token.start : token.end + 1]
            for match in pattern.finditer(fragment):
                offset = match.group(1).strip()
                if re.fullmatch(r"[+-]?\d+", offset):
                    continue
                start, end = token.start + match.start(1), token.start + match.end(1)
                try:
                    value = _integer_expression(offset)
                except ValueError:
                    value = None
                findings.append(self._finding("MACRO_ARITHMETIC", "time", "dml", sql, start, end, value))
        return findings

    def _functions(self, sql: str, masked: str, tokens: list[Any]) -> list[Finding]:
        findings = []
        for function, arity, rule, scope in (
            ("DateFormat", 1, "DATEFORMAT_ARITY", "time"),
            ("ConcatWithRank", 3, "CONCAT_RANK_ARITY", "sequence"),
        ):
            pattern = re.compile(rf"\bbicoredata\s*\.\s*{function}\s*\(", re.I)
            for match in pattern.finditer(masked):
                depth, commas, has_argument, end = 0, 0, False, match.end()
                for token in tokens:
                    if token.start < match.end() - 1:
                        continue
                    if token.token_type.name == "L_PAREN":
                        depth += 1
                    elif token.token_type.name == "R_PAREN":
                        depth -= 1
                        if depth == 0:
                            end = token.end + 1
                            break
                    elif depth == 1 and token.token_type.name == "COMMA":
                        commas += 1
                    if token.start >= match.end() and depth:
                        has_argument = True
                actual = commas + 1 if has_argument else 0
                if actual != arity:
                    findings.append(
                        self._finding(rule, scope, "dml", sql, match.start(), end, detail=f"实际 {actual}。")
                    )
        return findings

    def _artifacts(self, artifacts: dict[str, str], statements: dict[str, exp.Expression]) -> list[Finding]:
        ddl, dml = statements.get("ddl"), statements.get("dml")
        findings = []

        def add(rule: str, kind: str, detail: str = "", patch: tuple[int, int, str] | None = None) -> None:
            """Attach an artifact-level issue with a whole-statement location when no span is known."""
            sql = artifacts.get(kind, "")
            start, end, replacement = patch if patch is not None else (0, len(sql), None)
            findings.append(self._finding(rule, "engineering", kind, sql, start, end, replacement, detail))

        if table_name(ddl) != table_name(dml):
            add("TARGET_MISMATCH", "dml")
        columns = [item.name.lower() for item in ddl.this.expressions if isinstance(item, exp.ColumnDef)]
        if not columns or len(columns) != len(set(columns)):
            add("DDL_COLUMNS", "ddl")
        partition_property = ddl.find(exp.PartitionedByProperty)
        expected_partitions = []
        if partition_property is not None:
            expected_partitions = [item.name.lower() for item in partition_property.this.expressions]
        partition = dml.args.get("partition") or dml.this.args.get("partition")
        entries = partition.expressions if partition is not None else []
        actual = [item.left.name.lower() if isinstance(item, exp.EQ) else item.name.lower() for item in entries]
        if not expected_partitions or set(expected_partitions) != set(actual) or len(actual) != len(set(actual)):
            add("PARTITION_MISMATCH", "dml")
        dynamic = [item.name.lower() for item in entries if not isinstance(item, exp.EQ)]
        expected = columns + dynamic
        query = dml.expression
        if not isinstance(query, exp.Query):
            add("PROJECTION_ALIGNMENT", "dml")
        else:
            projections = query.selects
            actual_names = [item.alias_or_name.lower() for item in projections]
            if any(item.is_star for item in projections) or actual_names != expected:
                patch = _projection_patch(artifacts.get("dml", ""), query, actual_names, expected)
                uncertain = any(item.is_star for item in projections)
                uncertain = uncertain or (len(actual_names) == len(expected) and set(actual_names) != set(expected))
                rule = "PROJECTION_UNVERIFIED" if uncertain else "PROJECTION_ALIGNMENT"
                add(rule, "dml", f"期望列：{expected}，实际：{actual_names}。", patch)
            if query.args.get("with_") is not None:
                add("CTE_POSITION", "dml")
        if not ddl.args.get("exists") or ddl.find(exp.ExternalProperty) is None:
            sql = artifacts.get("ddl", "")
            code = _code_mask(sql, "hive")
            match = re.search(r"\bCREATE\s+(?:EXTERNAL\s+)?TABLE(?:\s+IF\s+NOT\s+EXISTS)?\b", code, re.I)
            patch = (*match.span(), "CREATE EXTERNAL TABLE IF NOT EXISTS") if match else None
            add("DDL_EXTERNAL", "ddl", patch=patch)
        storage = ddl.find(exp.FileFormatProperty)
        if storage is None or storage.this.name.upper() != "ORC":
            match = re.search(r"\bSTORED\s+AS\s+(\w+)", _code_mask(artifacts.get("ddl", ""), "hive"), re.I)
            patch = (*match.span(1), "ORC") if match else None
            add("DDL_STORAGE", "ddl", patch=patch)
        return findings

    def _window_checks(self, sql: str, statement: exp.Expression) -> list[Finding]:
        findings = []
        for window in statement.find_all(exp.Window):
            order = window.args.get("order")
            if not isinstance(window.this, exp.RowNumber) or order is None:
                continue
            partitions = {item.sql() for item in window.args.get("partition_by", [])}
            keys = [item.this for item in order.expressions]
            if keys and all(isinstance(key, exp.Literal) or key.sql() in partitions for key in keys):
                findings.append(self._finding("CONSTANT_WINDOW_ORDER", "grain_join", "dml", sql, 0, len(sql)))
        return findings


def _code_mask(sql: str, dialect: str) -> str:
    code = [" "] * len(sql)
    for token in sqlglot.Dialect.get_or_raise(dialect).tokenize(sql):
        if token.token_type.name not in {"STRING", "IDENTIFIER"}:
            code[token.start : token.end + 1] = sql[token.start : token.end + 1]
    return "".join(code)


def _projection_patch(
    sql: str, query: exp.Expression, actual: list[str], expected: list[str]
) -> tuple[int, int, str] | None:
    if not isinstance(query, exp.Select) or len(actual) != len(set(actual)) or set(actual) != set(expected):
        return None
    if query.args.get("distinct") or query.args.get("hint"):
        return None
    for clause in (query.args.get("group"), query.args.get("order")):
        if clause is not None and any(isinstance(node, exp.Literal) for node in clause.walk()):
            return None
    depth, start, boundaries, end = 0, None, [], None
    for token in sqlglot.Dialect.get_or_raise("spark").tokenize(sql):
        kind = token.token_type.name
        if kind == "L_PAREN":
            depth += 1
        elif kind == "R_PAREN":
            depth -= 1
        elif depth == 0:
            if kind == "SELECT":
                start = token.end + 1
                boundaries = [start]
            elif start is not None and kind == "COMMA":
                boundaries.append(token.start)
                boundaries.append(token.end + 1)
            elif start is not None and kind == "FROM":
                end = token.start
                boundaries.append(end)
                break
    if start is None or end is None or len(boundaries) != len(actual) * 2:
        return None
    fragments = [sql[left:right].strip() for left, right in zip(boundaries[::2], boundaries[1::2], strict=True)]
    # Avoid moving a line comment so that it consumes a neighbouring projection.
    if any("--" in fragment or "/*" in fragment for fragment in fragments):
        return None
    mapping = dict(zip(actual, fragments, strict=True))
    return start, end, " " + ", ".join(mapping.get(name, "") for name in expected) + " "


def _integer_expression(text: str) -> str:
    if len(text) > 80:
        raise ValueError("Macro expression is too long")

    def evaluate(node: ast.AST) -> int:
        """Evaluate only bounded integer arithmetic used in scheduling offsets."""
        if isinstance(node, ast.Constant) and type(node.value) is int:
            result = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            left, right = evaluate(node.left), evaluate(node.right)
            result = left + right if isinstance(node.op, ast.Add) else left - right
            if isinstance(node.op, ast.Mult):
                result = left * right
        else:
            raise ValueError("Only bounded integer addition, subtraction and multiplication are supported")
        if abs(result) > 1_000_000:
            raise ValueError("Macro offset exceeds supported bound")
        return result

    try:
        return str(evaluate(ast.parse(text, mode="eval").body))
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("Unsupported macro expression") from exc
