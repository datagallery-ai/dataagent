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
"""Built-in SQL AST hard-reject rules (R1–R3).

Extend later by appending a rule object with ``rule_id`` + ``check(ast, ctx)``
to ``DEFAULT_RULES`` — no extra registry layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Violation:
    """One hard-reject rule violation with optional SQL fragment."""

    rule_id: str
    message: str
    fragment: str
    suggestion: str

    def to_dict(self) -> dict[str, str]:
        """Return a plain dict for JSON / error payloads."""
        return asdict(self)


@dataclass(frozen=True)
class RuleContext:
    """Dialect hint passed into rule checkers."""

    dialect: str | None = None


@dataclass(frozen=True)
class Rule:
    """One hard-reject rule. Add more by appending to ``DEFAULT_RULES``."""

    rule_id: str
    check: Callable[[Any, RuleContext], list[Violation]]


def _fragment(node: Any, ctx: RuleContext) -> str:
    """Render an AST node to SQL for violation fragments."""
    return node.sql(dialect=ctx.dialect) if ctx.dialect else node.sql()


def _unwrap(expr: Any, exp: Any) -> Any:
    """Strip wrapping parentheses from an expression."""
    node = expr
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _literal_value(node: Any, exp: Any) -> Any | None:
    """Return Python value of a literal node, or None if not a literal."""
    node = _unwrap(node, exp)
    if not isinstance(node, exp.Literal):
        return None
    if node.is_string:
        return node.this
    try:
        text = str(node.this)
        return float(text) if "." in text else int(text)
    except (TypeError, ValueError):
        return None


def _const_bool(expr: Any, exp: Any) -> bool | None:
    """Small constant folder for ON predicates (TRUE / 1=1 / NOT FALSE / …)."""
    if expr is None:
        return None
    node = _unwrap(expr, exp)
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return float(node.this) != 0.0
        except (TypeError, ValueError):
            return None
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        left, right = _literal_value(node.this, exp), _literal_value(node.expression, exp)
        if left is None or right is None:
            return None
        try:
            if isinstance(node, exp.EQ):
                return left == right
            if isinstance(node, exp.NEQ):
                return left != right
            if isinstance(node, exp.GT):
                return left > right
            if isinstance(node, exp.GTE):
                return left >= right
            if isinstance(node, exp.LT):
                return left < right
            return left <= right
        except TypeError:
            return None
    if isinstance(node, exp.Not):
        inner = _const_bool(node.this, exp)
        return None if inner is None else (not inner)
    if isinstance(node, exp.Or):
        left, right = _const_bool(node.this, exp), _const_bool(node.expression, exp)
        if left is True or right is True:
            return True
        if left is False and right is False:
            return False
        return None
    if isinstance(node, exp.And):
        left, right = _const_bool(node.this, exp), _const_bool(node.expression, exp)
        if left is False or right is False:
            return False
        if left is True and right is True:
            return True
        return None
    return None


def _check_r1(ast: Any, ctx: RuleContext) -> list[Violation]:
    """R1: CROSS JOIN or constant-true ON (cartesian)."""
    from sqlglot import exp

    out: list[Violation] = []
    for join in ast.find_all(exp.Join):
        kind = (join.args.get("kind") or "").upper()
        on_expr = join.args.get("on")
        is_cross = kind == "CROSS"
        is_tautology = (not is_cross) and _const_bool(on_expr, exp) is True
        if not is_cross and not is_tautology:
            continue
        out.append(
            Violation(
                rule_id="R1",
                message=(
                    "JOIN ON a constant-true predicate is a cartesian product and is forbidden."
                    if is_tautology
                    else "CROSS JOIN can produce unbounded cartesian products and is forbidden."
                ),
                fragment=_fragment(join, ctx),
                suggestion=(
                    "Rewrite with an explicit JOIN ... ON / USING that states the association, "
                    "or pre-aggregate / filter each side before combining."
                ),
            )
        )
    return out


def _check_r2(ast: Any, ctx: RuleContext) -> list[Violation]:
    """R2: JOIN without ON/USING, or NATURAL JOIN."""
    from sqlglot import exp

    out: list[Violation] = []
    for join in ast.find_all(exp.Join):
        kind = (join.args.get("kind") or "").upper()
        if kind == "CROSS":
            continue  # R1
        method = (join.args.get("method") or "").upper()
        has_on = join.args.get("on") is not None
        has_using = bool(join.args.get("using"))
        if method == "NATURAL":
            out.append(
                Violation(
                    rule_id="R2",
                    message="NATURAL JOIN implicitly matches columns and is forbidden.",
                    fragment=_fragment(join, ctx),
                    suggestion="Replace NATURAL JOIN with JOIN ... ON / USING that lists the intended keys.",
                )
            )
            continue
        if has_on or has_using:
            continue
        out.append(
            Violation(
                rule_id="R2",
                message="JOIN without ON/USING (including comma joins) is forbidden.",
                fragment=_fragment(join, ctx),
                suggestion=(
                    "Add an explicit JOIN ... ON <predicate> or JOIN ... USING (...); "
                    "do not rely on WHERE alone for association."
                ),
            )
        )
    return out


def _is_number_literal(node: Any, exp: Any) -> bool:
    """True if ``node`` is a numeric (non-string) literal."""
    return isinstance(node, exp.Literal) and not node.is_string


def _is_column_ref(node: Any, exp: Any) -> bool:
    """True if ``node`` is a column/identifier, optionally +/- a number literal."""
    if node is None:
        return False
    node = _unwrap(node, exp)
    if isinstance(node, (exp.Column, exp.Identifier)):
        return True
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        left, right = node.this, node.expression
        return (_is_column_ref(left, exp) and _is_number_literal(right, exp)) or (
            _is_column_ref(right, exp) and _is_number_literal(left, exp)
        )
    return False


def _where_has_column_bound(where: Any, exp: Any) -> bool:
    """True if WHERE has a column compared against a numeric literal bound."""
    root = where.this if isinstance(where, exp.Where) else where
    if root is None:
        return False
    for node in root.find_all(exp.LT, exp.LTE, exp.GT, exp.GTE):
        left, right = node.this, node.expression
        if isinstance(node, (exp.LT, exp.LTE)) and _is_number_literal(right, exp) and _is_column_ref(left, exp):
            return True
        if isinstance(node, (exp.GT, exp.GTE)) and _is_number_literal(left, exp) and _is_column_ref(right, exp):
            return True
    return False


def _refs_cte(node: Any, cte_name: str, exp: Any) -> bool:
    """True if ``node`` references a table named ``cte_name``."""
    return any((table.name or "").lower() == cte_name for table in node.find_all(exp.Table))


def _recursive_cte_bounded(cte: Any, cte_name: str, exp: Any) -> bool:
    """True if recursive CTE member has LIMIT or a numeric column bound."""
    body = cte.this
    if isinstance(body, exp.Union) and body.args.get("limit") is not None and _refs_cte(body, cte_name, exp):
        return True
    for select in cte.find_all(exp.Select):
        if not _refs_cte(select, cte_name, exp):
            continue
        if select.args.get("limit") is not None:
            return True
        where = select.args.get("where")
        if where is not None and _where_has_column_bound(where, exp):
            return True
    return False


def _check_r3(ast: Any, ctx: RuleContext) -> list[Violation]:
    """R3: WITH RECURSIVE without LIMIT or column numeric bound on recursive member."""
    from sqlglot import exp

    out: list[Violation] = []
    for with_expr in ast.find_all(exp.With):
        if not with_expr.args.get("recursive"):
            continue
        for cte in with_expr.expressions or []:
            if not isinstance(cte, exp.CTE):
                continue
            cte_name = (cte.alias_or_name or "").lower()
            if not cte_name or _recursive_cte_bounded(cte, cte_name, exp):
                continue
            out.append(
                Violation(
                    rule_id="R3",
                    message=(
                        "WITH RECURSIVE without a recognizable termination or depth bound "
                        "is forbidden (default-deny; AST cannot prove termination)."
                    ),
                    fragment=_fragment(cte, ctx),
                    suggestion=(
                        "Add a termination predicate on the recursive member "
                        "(e.g. WHERE depth < N) and/or a LIMIT on that member."
                    ),
                )
            )
    return out


# Built-ins. Extend: DEFAULT_RULES.append(Rule("R4", _check_r4))
DEFAULT_RULES: list[Rule] = [
    Rule("R1", _check_r1),
    Rule("R2", _check_r2),
    Rule("R3", _check_r3),
]


def run_ast_rules(ast: Any, ctx: RuleContext) -> list[Violation]:
    """Run all ``DEFAULT_RULES`` on ``ast`` and collect violations."""
    found: list[Violation] = []
    for rule in DEFAULT_RULES:
        found.extend(rule.check(ast, ctx))
    return found
