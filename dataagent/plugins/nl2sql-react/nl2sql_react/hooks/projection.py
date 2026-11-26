"""Deterministic projection comparison between a locked contract and an answer SQL.

The contract records the fields the request asks to see. This module compares that
declared list with what the answer SQL actually projects, so an extra ranking key or
filter basis is caught by code rather than inferred from SQL structure.

Nothing here decides correctness. A disagreement is a question: either the projection
carries a field the request did not ask for, or the contract declaration was
incomplete. The model resolves it by editing one of the two.
"""

import re

import sqlglot
from sqlglot import exp

_WORD = re.compile(r"[a-z0-9]+")


def parse(sql):
    try:
        return sqlglot.parse_one(sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return None


def outer_select(tree):
    if isinstance(tree, exp.Select):
        return tree
    return tree.find(exp.Select) if tree is not None else None


def projection_names(sql):
    """Model-facing names for each projected expression, in SELECT order."""
    tree = parse(sql)
    select = outer_select(tree) if tree is not None else None
    if select is None:
        return None
    names = []
    for index, node in enumerate(select.expressions, start=1):
        if isinstance(node, exp.Alias):
            names.append(node.alias_or_name)
        elif isinstance(node, exp.Column):
            names.append(node.name)
        elif isinstance(node, (exp.Star,)) or (
            isinstance(node, exp.Column) and isinstance(node.this, exp.Star)
        ):
            names.append("*")
        else:
            names.append(f"expression_{index}")
    return names


def tokens(text):
    """Comparable word set, so `school_name` and `school name` match."""
    return set(_WORD.findall(text.casefold()))


def _matches(declared, projected):
    """True when one declaration and one projection name the same field.

    Exact token equality is required, plus containment for the common case where the
    projection carries a table prefix or a qualifier the declaration omits.
    """
    a, b = tokens(declared), tokens(projected)
    if not a or not b:
        return False
    if a == b:
        return True
    return a <= b or b <= a


def _order_changed(declared, projected):
    """Check order when each output can be identified uniquely."""
    expected = [tokens(field) for field in declared]
    positions = []
    for name in projected:
        actual = tokens(name)
        candidates = [index for index, field in enumerate(expected) if actual and field == actual]
        if not candidates:
            candidates = [index for index, field in enumerate(declared) if _matches(field, name)]
        if len(candidates) != 1:
            return False
        positions.append(candidates[0])
    return len(set(positions)) == len(expected) and positions != list(range(len(expected)))


def compare(declared, sql):
    """Return missing fields, extra fields, and the expected/actual order if different.

    `missing` lists declared fields the projection does not appear to carry.
    `extra` lists projected fields no declaration accounts for.
    The order diagnostic is only available for an unambiguous permutation.
    """
    projected = projection_names(sql)
    if projected is None or not declared:
        return [], [], None
    if "*" in projected:
        # A star projection has no stable names to compare; the star check reports it.
        return [], [], None
    if len(declared) == len(projected) and _order_changed(declared, projected):
        return [], [], (declared, projected)
    unmatched_declared = list(declared)
    extra = []
    for name in projected:
        for index, field in enumerate(unmatched_declared):
            if _matches(field, name):
                unmatched_declared.pop(index)
                break
        else:
            extra.append(name)
    return unmatched_declared, extra, None


def describe(missing, extra, order=None):
    """A single advisory sentence for the model, or None when nothing differs."""
    if order is not None:
        expected, actual = order
        return (
            f"The answer's column order is [{', '.join(actual)}]. "
            f"The contract expects [{', '.join(expected)}]. "
            "Project those fields in the requested order."
        )
    parts = []
    if extra:
        parts.append(
            "the answer projects " + ", ".join(extra) + " which the contract does not list"
        )
    if missing:
        parts.append(
            "the contract lists " + ", ".join(missing) + " which the answer does not project"
        )
    if not parts:
        return None
    return (
        "; ".join(parts) + ". The contract records the fields the request asks to see, "
        "not every value the SQL computes: a ranking key, tie-break column or filter "
        "basis is computed without being shown. Either project exactly the declared "
        "fields, or, if the request really does ask for another field, change the "
        "contract with fresh evidence and a reason."
    )
