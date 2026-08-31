from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

_ANALYZER_ROOT = Path(__file__).resolve().parents[3] / "scripts" / "analyzer"


def _production_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(_ANALYZER_ROOT.rglob("*.py")):
        files.append(path)
    return files


def _method_category(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    if node.name == "__new__":
        return 1
    if node.name == "__init__":
        return 2
    if node.name == "__post_init__":
        return 3
    if node.name.startswith("__") and node.name.endswith("__"):
        return 4

    decorators: set[str] = set()
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name):
            decorators.add(decorator.id)
        if isinstance(decorator, ast.Attribute):
            decorators.add(decorator.attr)
    if decorators.intersection({"property", "setter", "deleter"}):
        return 5
    if "staticmethod" in decorators:
        return 6
    if "classmethod" in decorators:
        return 7
    if node.name.startswith("_"):
        return 9
    return 8


def _uses_pep604_none(node: ast.AST) -> bool:
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.BitOr):
        return False
    return any(isinstance(child, ast.Constant) and child.value is None for child in ast.walk(node))


class _PrivateAccessVisitor(ast.NodeVisitor):
    def __init__(self, class_names: set[str]) -> None:
        self.class_names = class_names
        self.class_stack: list[str] = []
        self.violations: list[tuple[int, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Track the class that owns explicit private member access."""
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Reject access to another analyzer class's private member."""
        if isinstance(node.value, ast.Name) and node.value.id in self.class_names:
            current_class: Optional[str] = self.class_stack[-1] if self.class_stack else None
            is_private = node.attr.startswith("_") and not node.attr.startswith("__")
            if is_private and current_class != node.value.id:
                self.violations.append((node.lineno, f"{node.value.id}.{node.attr}"))
        self.generic_visit(node)


def test_analyzer_class_method_order() -> None:
    """Analyzer classes must follow the repository's required method ordering."""
    violations: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            previous_category = 0
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                category = _method_category(item)
                if category < previous_category:
                    violations.append(f"{path.name}:{item.lineno} {node.name}.{item.name}")
                previous_category = max(previous_category, category)
    assert not violations, "Method order violations:\n" + "\n".join(violations)


def test_analyzer_source_conventions() -> None:
    """Analyzer production code must satisfy the mechanical Python conventions."""
    violations: list[str] = []
    for path in _production_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for line_number, line in enumerate(source.splitlines(), 1):
            if len(line) > 120:
                violations.append(f"{path.name}:{line_number} line length {len(line)}")
        for node in ast.walk(tree):
            is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_function and not node.name.startswith("_") and ast.get_docstring(node, clean=False) is None:
                violations.append(f"{path.name}:{node.lineno} public function lacks docstring: {node.name}")
            if isinstance(node, ast.ListComp):
                violations.append(f"{path.name}:{node.lineno} list comprehension")
            if isinstance(node, ast.Lambda):
                violations.append(f"{path.name}:{node.lineno} lambda expression")
            if _uses_pep604_none(node):
                violations.append(f"{path.name}:{node.lineno} use Optional instead of | None")
            is_value_read = isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
            is_string_key = is_value_read and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)
            if is_string_key:
                violations.append(f"{path.name}:{node.lineno} dictionary value read via []")
    assert not violations, "Source convention violations:\n" + "\n".join(violations)


def test_analyzer_does_not_access_other_classes_private_members() -> None:
    """Analyzer modules must use public APIs when crossing class boundaries."""
    parsed: dict[Path, ast.Module] = {}
    class_names: set[str] = set()
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parsed[path] = tree
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_names.add(node.name)

    violations: list[str] = []
    for path, tree in parsed.items():
        visitor = _PrivateAccessVisitor(class_names)
        visitor.visit(tree)
        for line_number, member in visitor.violations:
            violations.append(f"{path.name}:{line_number} {member}")
    assert not violations, "Cross-class private member access:\n" + "\n".join(violations)
