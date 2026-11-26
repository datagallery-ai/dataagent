"""Deterministic, advisory checks on a drafted answer SQL.

Every finding is a question the model should be able to answer from the original
request, never a rewrite and never a block. A finding means "look again", not "wrong".
Rules reference only SQL structure and the user's wording, never a database or column name.
"""

import re

import sqlglot
from sqlglot import exp

ROUND_WORDS = re.compile(
    r"\b(round(ed|ing)?|decimal|decimals|places?|precision|nearest|whole number|integer)\b", re.I
)
PERCENT_WORDS = re.compile(r"(percent|percentage|%)", re.I)
DISTINCT_WORDS = re.compile(r"\b(distinct|unique|different|how many (?:different|unique))\b", re.I)


def parse(sql):
    try:
        return sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return None


def projections(tree):
    return list(tree.expressions) if isinstance(tree, exp.Select) else []


def outer_select(tree):
    if isinstance(tree, exp.Select):
        return tree
    return tree.find(exp.Select) if tree is not None else None


def check_rounding(tree, question):
    if ROUND_WORDS.search(question):
        return None
    if any(isinstance(node, exp.Round) for node in tree.find_all(exp.Round)):
        return (
            "SQL rounds a value, but the request names no precision. Keep full precision "
            "unless the request or evidence asks for rounding."
        )
    return None


def check_integer_division(tree):
    for node in tree.find_all(exp.Div):
        left, right = node.left, node.right
        if any(
            isinstance(side, exp.Cast)
            or (isinstance(side, exp.Literal) and "." in side.name)
            or (
                isinstance(side, exp.Mul)
                and any(
                    isinstance(literal, exp.Literal) and "." in literal.name
                    for literal in side.find_all(exp.Literal)
                )
            )
            for side in (left, right)
        ):
            continue
        if isinstance(left, exp.Count) or isinstance(left, exp.Sum) or isinstance(right, exp.Count):
            return (
                "A division has COUNT/SUM operands and no REAL cast or decimal literal. "
                "SQLite divides integers as integers, which silently truncates a ratio."
            )
    return None


def check_count_fanout(tree, question):
    # Only flag when the request itself asks for distinct entities. A blanket warning on
    # every joined COUNT(*) also fires on correct answers, so it is not useful as a prompt.
    if not DISTINCT_WORDS.search(question):
        return None
    select = outer_select(tree)
    if select is None or not (select.args.get("joins") or []):
        return None
    for count in select.find_all(exp.Count):
        if count.args.get("distinct") or isinstance(count.this, exp.Distinct):
            continue
        if isinstance(count.this, exp.Star) or count.this is None:
            return (
                "The request asks for distinct or unique entities, but the SQL counts joined rows "
                "with COUNT(*). A join can pair one entity with several rows, so the count is not "
                "the entity count. Count the entity key with DISTINCT or qualify with EXISTS."
            )
    return None


def check_star_projection(tree):
    select = outer_select(tree)
    if select is None:
        return None
    if any(
        isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
        for e in select.expressions
    ):
        return "The answer selects every column. Return only the fields the request asks for."
    return None


def check_inner_join_for_all(tree, question):
    if not re.search(r"\b(every|each|all|including|regardless)\b", question, re.I):
        return None
    if not re.search(
        r"\b(zero|none|no |without|missing|not (?:have|had)|regardless)\b", question, re.I
    ):
        return None
    select = outer_select(tree)
    if select is None:
        return None
    for join in select.args.get("joins") or []:
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if not side and kind != "CROSS":
            return (
                "The request says every/all entities including those with none, but the SQL uses an "
                "INNER JOIN, which drops entities that have no matching row. Drive from the entity "
                "relation with LEFT JOIN and decide how an empty group should read."
            )
    return None


def lint(sql, question):
    tree = parse(sql)
    if tree is None:
        return []
    findings = []
    for result in (
        check_star_projection(tree),
        check_rounding(tree, question),
        check_integer_division(tree),
        check_count_fanout(tree, question),
        check_inner_join_for_all(tree, question),
    ):
        if result:
            findings.append(result)
    return findings
