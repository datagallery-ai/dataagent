"""Scoped SQL patterns and metadata evidence, without model-based discovery."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

INTEGER_TYPES = {"TINYINT", "SMALLINT", "INT", "BIGINT"}
FLOAT_TYPES = {"FLOAT", "DOUBLE", "DECIMAL"}


def load_schema_evidence(workspace: Path) -> tuple[dict, dict]:
    """Read only the visible schema JSON export; never execute its assignment prefix."""
    source = workspace / "schema_schemair.md"
    if not source.is_file():
        return {}, {"status": "missing", "source": source.name}
    if not source.resolve().is_relative_to(workspace.resolve()) or source.stat().st_size > 2_000_000:
        return {}, {"status": "unsupported", "source": source.name}
    text = source.read_text(encoding="utf-8")
    evidence = {"source": source.name, "sha256": hashlib.sha256(text.encode()).hexdigest()}
    try:
        payload = re.sub(r"^\s*schema_ir\s*=\s*", "", text).lstrip()
        data, end = json.JSONDecoder().raw_decode(payload)
        trailing = payload[end:].strip()
        if trailing:
            if not trailing.startswith("join_relations"):
                raise ValueError("Unsupported schema suffix")
            relations = re.sub(r"^join_relations\s*=\s*", "", trailing)
            if not isinstance(json.loads(relations), list):
                raise ValueError("Invalid join relations")
        if not isinstance(data, dict):
            raise ValueError("Expected a schema object")
        tables = {
            name.lower(): table
            for name, table in data.items()
            if isinstance(table, dict) and isinstance(table.get("columns", {}), dict)
        }
        return tables, {**evidence, "status": "loaded", "table_count": len(tables)}
    except ValueError:
        return {}, {**evidence, "status": "unverified"}


class QueryChecks:
    """Inspect one SQL block at a time and return every matched rule issue."""

    def __init__(self, sql: str, statement: exp.Expression, emit: Callable, schema: dict) -> None:
        """Bind original SQL, canonical finding construction and optional metadata."""
        self.sql = sql
        self.statement = statement
        self.emit = emit
        self.schema = schema
        self.findings: list[Any] = []
        self.tokens = sqlglot.Dialect.get_or_raise("spark").tokenize(sql)
        self.scopes = list(traverse_scope(self.statement))

    def detect(self) -> list[Any]:
        """Return local evidence-backed findings without changing the SQL or invoking an LLM."""
        for scope in self.scopes:
            if not isinstance(scope.expression, exp.Select):
                continue
            self._sequence(scope)
            self._filters(scope)
            self._time(scope)
            self._metrics(scope)
            self._columns_and_joins(scope)
        return list({item.issue_id: item for item in self.findings}.values())

    def _add(
        self,
        rule: str,
        category: str,
        node: exp.Expression,
        detail: str = "",
        patch: tuple[int, int, str] | None = None,
    ) -> None:
        positions = [part.meta for part in node.walk() if "start" in part.meta and "end" in part.meta]
        start = min((item.get("start") for item in positions), default=0)
        end = max((item.get("end") + 1 for item in positions), default=len(self.sql))
        replacement = None
        if patch is not None:
            start, end, replacement = patch
        detail = f"{detail} 表达式：{node.sql(dialect='spark')}；行 {self.sql.count(chr(10), 0, start) + 1}。"
        self.findings.append(self.emit(rule, category, "dml", self.sql, start, end, replacement, detail))

    def _nodes(self, scope: Scope, kind: type[exp.Expression]) -> list[exp.Expression]:
        return [node for node in scope.expression.find_all(kind) if node.find_ancestor(exp.Select) is scope.expression]

    def _source(self, scope: Scope, column: exp.Column) -> Any:
        sources = dict(scope.selected_sources)
        if column.table:
            pair = next((pair for name, pair in sources.items() if name.lower() == column.table.lower()), None)
            return pair[1] if pair else None
        if len(sources) == 1:
            return next(iter(sources.values()))[1]
        candidates = []
        for _, source in sources.values():
            if isinstance(source, Scope):
                outputs = source.outer_columns or source.expression.named_selects
                if not outputs or "*" in outputs:
                    return None
            else:
                table = ".".join(part.name.lower() for part in source.parts) if isinstance(source, exp.Table) else ""
                outputs = self.schema.get(table, {}).get("columns", {})
                if not outputs:
                    return None
            if column.name.lower() in {name.lower() for name in outputs}:
                candidates.append(source)
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _resolve(self, scope: Scope, node: exp.Expression, depth: int = 0) -> tuple[exp.Expression, Scope]:
        if depth > 8 or not isinstance(node, exp.Column):
            return node, scope
        source = self._source(scope, node)
        if isinstance(source, Scope) and isinstance(source.expression, exp.Select):
            outputs = [item for item in source.expression.selects if item.alias_or_name.lower() == node.name.lower()]
            if len(outputs) == 1:
                value = outputs[0].this if isinstance(outputs[0], exp.Alias) else outputs[0]
                return self._resolve(source, value, depth + 1)
        return node, scope

    def _column_info(self, scope: Scope, node: exp.Expression) -> dict:
        node, scope = self._resolve(scope, node)
        if isinstance(node, exp.Cast):
            return {"value_type": node.args.get("to").sql(), "source": "SQL CAST"}
        if not isinstance(node, exp.Column):
            return {}
        source = self._source(scope, node)
        if not isinstance(source, exp.Table):
            return {}
        table = ".".join(part.name.lower() for part in source.parts)
        columns = self.schema.get(table, {}).get("columns", {})
        info = next((value for name, value in columns.items() if name.lower() == node.name.lower()), {})
        return {**info, "source": f"schema_schemair.md:{table}.{node.name}"} if isinstance(info, dict) and info else {}

    def _sequence(self, scope: Scope) -> None:
        for call in self._nodes(scope, exp.Anonymous):
            if call.name.lower() != "concatwithrank" or not isinstance(call.parent, exp.Dot):
                continue
            if call.parent.this.name.lower() != "bicoredata":
                continue
            if scope.expression.args.get("group") is None and call.find_ancestor(exp.Window) is None:
                self._add("SEQUENCE_GROUP_BY", "sequence", call)
            if len(call.expressions) != 3:
                continue
            rank, origin_scope = self._resolve(scope, call.expressions[1])
            scaled = isinstance(rank, exp.Cast) and rank.args.get("to").sql().upper() in INTEGER_TYPES
            if scaled and rank.this.find(exp.Mul) is not None:
                self._add("SEQUENCE_SCALED_RANK", "sequence", rank)
            if not self._rank_known(origin_scope, rank):
                self._add("SEQUENCE_RANK_ORIGIN", "sequence", call.expressions[1])
        for aggregate in self._nodes(scope, exp.ArrayAgg):
            if isinstance(aggregate.this, exp.Order):
                self._add("SEQUENCE_UNSTABLE_AGG", "sequence", aggregate)

    def _is_position(self, scope: Scope, node: exp.Expression) -> bool:
        if not isinstance(node, exp.Column):
            return False
        for lateral in scope.expression.args.get("laterals", []):
            alias = lateral.args.get("alias")
            columns = alias.args.get("columns", []) if alias is not None else []
            position = isinstance(lateral.this, exp.Posexplode) and columns
            if (
                position
                and columns[0].name.lower() == node.name.lower()
                and (not node.table or node.table == lateral.alias)
            ):
                return True
        return False

    def _rank_known(self, scope: Scope, node: exp.Expression, depth: int = 0) -> bool:
        if depth > 12:
            return False
        if isinstance(node, exp.Window):
            order = node.args.get("order")
            return isinstance(node.this, (exp.RowNumber, exp.Rank, exp.DenseRank)) and order is not None
        if isinstance(node, exp.Case):
            values = [item.args.get("true") for item in node.args.get("ifs", [])]
            values.append(node.args.get("default"))
            values = [value for value in values if value is not None and not isinstance(value, exp.Null)]
            return bool(values) and all(self._rank_known(scope, value, depth + 1) for value in values)
        if isinstance(node, exp.If):
            values = [node.args.get("true"), node.args.get("false")]
            values = [value for value in values if value is not None and not isinstance(value, exp.Null)]
            return bool(values) and all(self._rank_known(scope, value, depth + 1) for value in values)
        if isinstance(node, exp.Cast) and node.args.get("to").sql().upper() in INTEGER_TYPES:
            return self._rank_known(scope, node.this, depth + 1)
        if isinstance(node, (exp.Add, exp.Sub)) and isinstance(node.expression, exp.Literal):
            return not node.expression.is_string and self._rank_known(scope, node.this, depth + 1)
        if not isinstance(node, exp.Column):
            return False
        if self._is_position(scope, node):
            return True
        source = self._source(scope, node)
        if not isinstance(source, Scope):
            return False
        names = source.outer_columns or source.expression.named_selects
        matches = [index for index, name in enumerate(names) if name.lower() == node.name.lower()]
        return len(matches) == 1 and self._rank_output(source, matches[0], depth + 1)

    def _rank_output(self, scope: Scope, index: int, depth: int) -> bool:
        if depth > 12:
            return False
        if isinstance(scope.expression, exp.Union):
            return bool(scope.union_scopes) and all(
                self._rank_output(child, index, depth + 1) for child in scope.union_scopes
            )
        if isinstance(scope.expression, exp.Select) and index < len(scope.expression.selects):
            value = scope.expression.selects[index]
            return self._rank_known(scope, value.this if isinstance(value, exp.Alias) else value, depth + 1)
        return False

    def _filters(self, scope: Scope) -> None:
        where = scope.expression.args.get("where")
        if where is None:
            return
        for window in self._nodes(scope, exp.Window):
            if not isinstance(window.this, exp.RowNumber) or not self._window_selected(window):
                continue
            keys = {
                column.sql().lower()
                for part in window.args.get("partition_by", [])
                for column in part.find_all(exp.Column)
            }
            order = window.args.get("order")
            if order is None or not any(item.args.get("desc") for item in order.expressions):
                continue
            time_order = False
            for item in order.expressions:
                info = self._column_info(scope, item.this)
                time_type = str(info.get("value_type", "")).upper() in {"DATE", "TIMESTAMP"}
                partition_time = isinstance(item.this, exp.Column) and item.this.name.lower().startswith("pt_")
                known_time = time_type or partition_time or _timestamp_unit(info)
                time_order = time_order or (bool(item.args.get("desc")) and bool(known_time))
            if not time_order:
                continue
            for predicate in _conjuncts(where.this):
                attributes = [column for column in predicate.find_all(exp.Column) if column.sql().lower() not in keys]
                # Window bounds define candidate snapshots; attribute filtering changes which row can win.
                attributes = [column for column in attributes if not column.name.lower().startswith("pt_")]
                if attributes:
                    detail = "属性过滤先于降序 ROW_NUMBER；需确认要取最新记录还是最新满足属性条件的记录。"
                    self._add("FILTER_BEFORE_LATEST", "null_filter", predicate, detail)

    def _window_selected(self, window: exp.Window) -> bool:
        for consumer in self.scopes:
            where = consumer.expression.args.get("where")
            if where is None:
                continue
            for term in _conjuncts(where.this):
                if not isinstance(term, exp.EQ):
                    continue
                for value, literal in ((term.this, term.expression), (term.expression, term.this)):
                    one = isinstance(literal, exp.Literal) and literal.this == "1" and not literal.is_string
                    if one and self._resolve(consumer, value)[0] is window:
                        return True
        return False

    def _time(self, scope: Scope) -> None:
        where = scope.expression.args.get("where")
        bounds: dict[str, dict] = {}
        if where is not None:
            for predicate in _conjuncts(where.this):
                if isinstance(predicate, (exp.GT, exp.GTE, exp.LT, exp.LTE)) and isinstance(predicate.this, exp.Column):
                    column = predicate.this
                    if not column.name.lower().startswith("pt_"):
                        continue
                    side = "lower" if isinstance(predicate, (exp.GT, exp.GTE)) else "upper"
                    entry = bounds.setdefault(column.sql().lower(), {})
                    entry.update({side: predicate})
            for entry in bounds.values():
                if len(entry) == 1:
                    self._add("PARTITION_WINDOW_BOUNDS", "time", next(iter(entry.values())))
        for node in self._nodes(scope, exp.Binary):
            if isinstance(node, (exp.Add, exp.Sub)):
                for cast in (node.this, node.expression):
                    integral = isinstance(cast, exp.Cast) and cast.args.get("to").sql().upper() in INTEGER_TYPES
                    if integral and isinstance(cast.this, exp.Column) and cast.this.name.lower() == "pt_d":
                        self._add("DATE_INTEGER_ARITHMETIC", "time", node)
            if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.Sub)):
                left, right = self._column_info(scope, node.this), self._column_info(scope, node.expression)
                units = {_timestamp_unit(left), _timestamp_unit(right)}
                if units == {"s", "ms"}:
                    detail = f"元数据单位不同：{left.get('source')} 与 {right.get('source')}。"
                    self._add("TIMESTAMP_UNIT_MIX", "time", node, detail)

    def _metrics(self, scope: Scope) -> None:
        for cast in self._nodes(scope, exp.Cast):
            if cast.args.get("to").sql().upper() not in INTEGER_TYPES:
                continue
            parent = cast.parent
            nonzero = isinstance(parent, exp.NEQ) and (_is_zero(parent.this) or _is_zero(parent.expression))
            aggregation = isinstance(parent, (exp.Sum, exp.Avg))
            if not nonzero and not aggregation:
                continue
            info = self._column_info(scope, cast.this)
            if str(info.get("value_type", "")).upper() in INTEGER_TYPES:
                continue
            floating = re.split(r"[<(]", str(info.get("value_type", "")).upper())[0] in FLOAT_TYPES
            detail = f"输入类型证据：{info.get('value_type', '未知')}，来源：{info.get('source', '待确认')}。"
            patch = self._cast_type_patch(cast) if floating else None
            self._add("INTEGER_TRUNCATION", "metrics", cast, detail, patch)
        # Division fallback and rounding stages are business choices. SQL alone
        # cannot establish an error or authorize changing them.
        for compare in self._nodes(scope, exp.Binary):
            if not isinstance(compare, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
                continue
            for column, other in ((compare.this, compare.expression), (compare.expression, compare.this)):
                if not isinstance(column, exp.Column) or not isinstance(other, exp.Literal) or other.is_string:
                    continue
                info = self._column_info(scope, column)
                if re.match(r"STRING|VARCHAR|CHAR", str(info.get("value_type", "")), re.I):
                    self._add("STRING_NUMERIC_COMPARISON", "metrics", compare, f"类型来源：{info.get('source')}。")

    def _cast_type_patch(self, cast: exp.Cast) -> tuple[int, int, str] | None:
        positions = [node.meta.get("start") for node in cast.this.walk() if "start" in node.meta]
        if not positions:
            return None
        anchor = min(positions)
        for index, token in enumerate(self.tokens):
            if token.text.upper() not in {"CAST", "TRY_CAST"} or token.start >= anchor:
                continue
            depth, type_token, closing = 0, None, None
            for offset, current in enumerate(self.tokens[index + 1 :], index + 1):
                if current.token_type.name == "L_PAREN":
                    depth += 1
                elif current.token_type.name == "R_PAREN":
                    depth -= 1
                    if depth == 0:
                        closing = current
                        break
                elif depth == 1 and current.token_type.name == "ALIAS":
                    type_token = self.tokens[offset + 1]
            if closing is None or type_token is None or not token.start < anchor < closing.end:
                continue
            parsed = sqlglot.parse_one(self.sql[token.start : closing.end + 1], read="spark")
            if parsed == cast and type_token.text.upper() in INTEGER_TYPES:
                return type_token.start, type_token.end + 1, "DOUBLE"
        return None

    def _columns_and_joins(self, scope: Scope) -> None:
        aliases = {item.alias.lower() for item in scope.expression.selects if item.alias}
        for column in scope.columns:
            if not column.table and column.name.lower() in aliases:
                continue
            source = self._source(scope, column)
            if isinstance(source, Scope):
                outputs = source.outer_columns or source.expression.named_selects
                missing = (
                    outputs and "*" not in outputs and column.name.lower() not in {name.lower() for name in outputs}
                )
                if missing and not any(item.is_star for item in source.expression.selects):
                    self._add("COLUMN_SCOPE", "engineering", column, f"上游显式输出列：{outputs}。")
            elif isinstance(source, exp.Table) and self.schema:
                table = ".".join(part.name.lower() for part in source.parts)
                columns = self.schema.get(table, {}).get("columns", {})
                if columns and column.name.lower() not in {name.lower() for name in columns}:
                    self._add(
                        "METADATA_COLUMN_UNKNOWN",
                        "engineering",
                        column,
                        f"召回元数据 {table} 未列出此字段，可能不完整。",
                    )
        for join in scope.expression.args.get("joins", []):
            on = join.args.get("on")
            if on is None:
                continue
            alias = join.this.alias_or_name
            source_pair = scope.selected_sources.get(alias)
            source = source_pair[1] if source_pair else None
            if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
                continue
            keys = {col.name.lower() for col in on.find_all(exp.Column) if col.table.lower() == alias.lower()}
            group = source.expression.args.get("group")
            if keys and group is not None:
                grouped = {item.name.lower() for item in group.expressions if isinstance(item, exp.Column)}
                if grouped - keys:
                    self._add(
                        "JOIN_KEY_UNIQUENESS",
                        "grain_join",
                        on,
                        f"右侧按 {sorted(grouped)} 聚合，关联只用 {sorted(keys)}。",
                    )


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _is_zero(node: exp.Expression) -> bool:
    return isinstance(node, exp.Literal) and not node.is_string and bool(re.fullmatch(r"0(?:\.0+)?", node.this))


def _timestamp_unit(info: dict) -> str | None:
    unit = str(info.get("unit", "")).lower()
    description = str(info.get("description", ""))
    if unit in {"ms", "millisecond", "milliseconds"} or re.search(r"毫秒(?:级)?时间戳|时间戳.{0,5}毫秒", description):
        return "ms"
    if unit in {"s", "second", "seconds"} or re.search(r"(?<!毫)秒(?:级)?时间戳|时间戳.{0,5}(?<!毫)秒", description):
        return "s"
    return None
