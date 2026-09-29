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
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""把 interrupt 权限收成 HITL 要的 interrupt_on。

图装配只调用 `_build_interrupt_on_from_permissions`。精确工具把归一后的
路径交给 `_check_fs_permission`，只有结果正好是 interrupt 才打断。批量工具
看搜索子树和规则锚点交不交；路径缺失，或归一成当前目录别名，则整棵树都算进去。
glob 的 pattern 还能把搜索根带离 path。本文件不执行工具，也不写审批文案。
"""

from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any, TypeAlias

from dataagent.core.middleware.filesystem.filesystem import FilesystemPermission, _check_fs_permission
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig

from dataagent.core.backends.utils import _glob_anchor, _paths_overlap, to_posix_path, validate_path

Predicate: TypeAlias = Callable[[Any], bool]

# 工具名、操作、路径参数、范围、glob 才有的 pattern 参数。顺序就是登记顺序。
_TOOL_ROWS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("ls", "read", "path", "bulk", None),
    ("read_file", "read", "file_path", "exact", None),
    ("write_file", "write", "file_path", "exact", None),
    ("edit_file", "write", "file_path", "exact", None),
    ("delete", "write", "file_path", "bulk", None),
    ("glob", "read", "path", "bulk", "pattern"),
    ("grep", "read", "path", "bulk", None),
)

_INTERRUPT = "interrupt"
_EXACT = "exact"
_CURRENT_DIR = "/."
_ROOT = "/"
_DECISIONS = ("approve", "edit", "reject", "respond")


def _interrupt_anchors(rules: list[FilesystemPermission], operation: str) -> list[str]:
    """工厂调用时就把 interrupt 规则的锚点算完。之后改规则列表看不见。"""
    anchors: list[str] = []
    for item in rules:
        if item.mode != _INTERRUPT or operation not in item.operations:
            continue
        anchors.extend(_glob_anchor(pattern) for pattern in item.paths)
    return anchors


def _pattern_reaches(raw_pattern: str, anchors: list[str]) -> bool:
    """绝对 pattern 用它自己的锚点；相对 pattern 只有跳出 `..` 才算够到。"""
    posix = to_posix_path(raw_pattern)
    if posix.startswith(_ROOT):
        located = _glob_anchor(raw_pattern)
        return any(_paths_overlap(located, anchor) for anchor in anchors)
    return ".." in PurePosixPath(posix).parts


def _exact_gate(rules: list[FilesystemPermission], operation: str, arg_name: str) -> Predicate:
    """闭包抓住原规则对象，调用时才问兄弟模块。"""

    def decide(call: Any) -> bool:
        raw = call.tool_call.get("args", {}).get(arg_name)
        if not isinstance(raw, str):
            return False
        try:
            normalized = validate_path(raw)
        except ValueError:
            return False
        verdict = _check_fs_permission(rules, operation, normalized)
        return verdict == _INTERRUPT

    return decide


def _bulk_gate(
    rules: list[FilesystemPermission],
    operation: str,
    arg_name: str,
    pattern_name: str | None,
) -> Predicate:
    """锚点先算好。空锚点直接否，不再看 pattern。"""
    anchors = _interrupt_anchors(rules, operation)

    def decide(call: Any) -> bool:
        if not anchors:
            return False
        args = call.tool_call.get("args", {})
        raw = args.get(arg_name)
        if not isinstance(raw, str):
            return raw is None
        try:
            normalized = validate_path(raw)
        except ValueError:
            return False
        if normalized == _CURRENT_DIR:
            normalized = _ROOT
        for anchor in anchors:
            if _paths_overlap(normalized, anchor):
                return True
        if pattern_name is None:
            return False
        raw_pattern = args.get(pattern_name)
        if not isinstance(raw_pattern, str):
            return False
        return _pattern_reaches(raw_pattern, anchors)

    return decide


def _make_fs_when_predicate(
    rules: list[FilesystemPermission],
    operation: str,
    path_arg_name: str,
    scope: str,
    pattern_arg_name: str | None = None,
) -> Predicate:
    """`"exact"` 走精确匹配，其余范围一律按批量子树处理。"""
    if scope == _EXACT:
        return _exact_gate(rules, operation, path_arg_name)
    return _bulk_gate(rules, operation, path_arg_name, pattern_arg_name)


def _build_interrupt_on_from_permissions(rules: list[FilesystemPermission]) -> dict[str, InterruptOnConfig]:
    """有 interrupt 规则时，为操作对得上的工具各做一条配置。"""
    if not any(item.mode == _INTERRUPT for item in rules):
        return {}
    decisions = list(_DECISIONS)
    chosen: dict[str, InterruptOnConfig] = {}
    for tool_name, operation, arg_name, scope, pattern_name in _TOOL_ROWS:
        covered = any(item.mode == _INTERRUPT and operation in item.operations for item in rules)
        if not covered:
            continue
        chosen[tool_name] = InterruptOnConfig(
            allowed_decisions=decisions,
            when=_make_fs_when_predicate(rules, operation, arg_name, scope, pattern_name),
        )
    return chosen
