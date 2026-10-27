# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Validate the word roots of a proposed table name or DDL column names."""

import copy
import re
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext

# A single lowercase identifier segment: starts with a letter, no leading/trailing
# or consecutive underscores.
_IDENTIFIER = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
# Mirror the underlying validator's _MAX_BATCH_QUERY_ITEMS: a single batch accepts
# at most this many unique roots, so column roots are chunked to stay within it.
_UNIQUE_ROOT_LIMIT = 20
_K_MIN, _K_MAX = 1, 20


def _check_common_params(k: int | None, llm_batch_size: int | None) -> str:
    if k is not None and (type(k) is not int or not _K_MIN <= k <= _K_MAX):
        return f"k 必须是 {_K_MIN} 到 {_K_MAX} 之间的整数。"
    if llm_batch_size is not None and (type(llm_batch_size) is not int or llm_batch_size <= 0):
        return "llm_batch_size 必须是正整数或 None。"
    return ""


def _forward_options(k: int | None, llm_batch_size: int | None) -> dict[str, int]:
    """Forward k/llm_batch_size only when supplied; otherwise defer to the validator."""
    options: dict[str, int] = {}
    if k is not None:
        options["k"] = k
    if llm_batch_size is not None:
        options["llm_batch_size"] = llm_batch_size
    return options


async def _validate_root_batch(
    roots: list[str],
    options: dict[str, int],
    _tool_context: ToolExecutionContext,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Validate one batch of roots against the word-root dictionary.

    Returns (results, "") on success, where results is aligned one-to-one with
    ``roots``. On any failure, including a skipped validation when the dictionary
    is missing, returns (None, error_msg); the message is conclusive and the
    caller must not treat the roots as passed.
    """
    try:
        # Delay the import: unit tests replace the underlying module in sys.modules,
        # and a top-level import would bind the real (possibly absent) dependency.
        from dataagent.actions.tools.local_tool.word_root_validator import validate_word_roots

        response = await validate_word_roots(
            items=[{"english_name": root} for root in roots],
            _tool_context=_tool_context,
            **options,
        )
    except Exception as exc:
        return None, f"词根校验不可用：{type(exc).__name__}: {exc}"

    if not isinstance(response, dict):
        return None, "词根接口未返回有效响应。"
    if response.get("validation_status") == "skipped":
        return None, "词根字典缺失，已跳过词根校验。"
    results = response.get("results")
    if not isinstance(results, list):
        return None, "词根接口未返回 results 数组。"
    if len(roots) != len(results):
        return None, f"词根校验结果数量不一致：期望 {len(roots)} 项，实际 {len(results)} 项。"

    for index, root in enumerate(roots):
        result = results[index]
        error_msg = ""
        if not isinstance(result, dict):
            error_msg = "词根校验返回结果异常。"
        elif type(result.get("index")) is not int or result["index"] != index:
            error_msg = "词根校验返回结果异常：结果索引与输入位置不一致。"
        elif result.get("english_name") != root:
            error_msg = "词根校验返回结果异常：结果英文名与输入不一致。"
        elif type(result.get("found")) is not bool:
            error_msg = "词根校验返回结果异常：结果缺少有效的 found。"
        elif result["found"] and result.get("match_type") not in ("exact", "regex"):
            error_msg = "词根校验返回结果异常：命中结果缺少有效的 match_type。"
        if error_msg:
            return None, f"词根 {index} ({root})：{error_msg}"
    return results, ""


async def validate_table_word_roots(
    table_name: str,
    k: int | None = None,
    llm_batch_size: int | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Validate a complete candidate table name against the word-root dictionary.

    Args:
        table_name: Lowercase table name, optionally qualified as db.table.
            Only the table part is split on underscores.
        k: Maximum candidate count per unmatched root, between 1 and 20.
            Omit or pass None to use the underlying validator's default.
        llm_batch_size: Number of unmatched word roots passed to the LLM in a single
            call. Optional positive integer; omit or pass None to use the
            underlying validator's default.

    Returns:
        pass: True only when every root has an exact or regex match.
        failed_roots: Original results for unmatched roots; passed roots omitted.
        error_msg: Empty for a completed validation, otherwise the parameter, service or
            response error. On error, failed_roots is empty and not conclusive.
            Results must be returned in input order.
    """
    name = table_name.strip() if isinstance(table_name, str) else ""
    if not re.fullmatch(rf"(?:{_IDENTIFIER}\.)?{_IDENTIFIER}", name):
        return {
            "pass": False,
            "failed_roots": [],
            "error_msg": (
                "表名应为 table 或 db.table，使用小写字母、数字和下划线，"
                "标识符以字母开头，不允许连续或首尾下划线。"
            ),
        }
    param_error = _check_common_params(k, llm_batch_size)
    if param_error:
        return {"pass": False, "failed_roots": [], "error_msg": param_error}

    roots = name.rsplit(".", 1)[-1].split("_")
    results, error_msg = await _validate_root_batch(
        roots, _forward_options(k, llm_batch_size), _tool_context
    )
    if results is None:
        return {"pass": False, "failed_roots": [], "error_msg": error_msg}

    failed_roots = [result for result in results if not result["found"]]
    return {"pass": not failed_roots, "failed_roots": failed_roots, "error_msg": ""}


async def validate_column_word_roots(
    columns: list[str],
    k: int | None = None,
    llm_batch_size: int | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Validate the word roots of every DDL column name before writing DDL.

    Each column is a bare lowercase identifier (no db. prefix) split on underscores.
    Roots are deduplicated across all columns and validated in chunks of at most
    20 unique roots, then mapped back so each column reports its own result.

    Args:
        columns: Non-empty list of lowercase column names. Each name uses letters,
            digits and underscores, starts with a letter, and has no consecutive or
            leading/trailing underscores.
        k: Maximum candidate count per unmatched root, between 1 and 20.
            Omit or pass None to use the underlying validator's default.
        llm_batch_size: Number of unmatched word roots passed to the LLM in a single
            call. Optional positive integer; omit or pass None to use the
            underlying validator's default.

    Returns:
        pass: True only when every root of every column has an exact or regex match.
        columns: One entry per input column, in input order, each with
            ``column``, ``pass`` and ``failed_roots`` (unmatched roots, with
            ``index`` rewritten to the root's position within that column).
        error_msg: Empty for a completed validation, otherwise the parameter, service
            or response error. On error, columns is empty and not conclusive.
    """
    if not isinstance(columns, list) or not columns:
        return {"pass": False, "columns": [], "error_msg": "columns 必须是非空列表。"}
    param_error = _check_common_params(k, llm_batch_size)
    if param_error:
        return {"pass": False, "columns": [], "error_msg": param_error}

    column_roots: list[tuple[str, list[str]]] = []
    for index, column in enumerate(columns):
        name = column.strip() if isinstance(column, str) else ""
        if not re.fullmatch(_IDENTIFIER, name):
            return {
                "pass": False,
                "columns": [],
                "error_msg": (
                    f"列名 {index} ({column!r}) 非法：应使用小写字母、数字和下划线，"
                    "以字母开头，不允许连续或首尾下划线。"
                ),
            }
        column_roots.append((name, name.split("_")))

    # Deduplicate roots across all columns, preserving first-seen order.
    unique_roots: list[str] = []
    seen: set[str] = set()
    for _, roots in column_roots:
        for root in roots:
            if root not in seen:
                seen.add(root)
                unique_roots.append(root)

    options = _forward_options(k, llm_batch_size)
    root_result: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(unique_roots), _UNIQUE_ROOT_LIMIT):
        chunk = unique_roots[offset : offset + _UNIQUE_ROOT_LIMIT]
        results, error_msg = await _validate_root_batch(chunk, options, _tool_context)
        if results is None:
            return {"pass": False, "columns": [], "error_msg": error_msg}
        for root, result in zip(chunk, results):
            root_result[root] = result

    column_reports: list[dict[str, Any]] = []
    all_pass = True
    for name, roots in column_roots:
        failed_roots: list[dict[str, Any]] = []
        for position, root in enumerate(roots):
            result = root_result[root]
            if not result["found"]:
                # Copy the shared root result so each column can carry its own
                # in-column position without mutating the deduplicated original.
                item = copy.deepcopy(result)
                item["index"] = position
                failed_roots.append(item)
        if failed_roots:
            all_pass = False
        column_reports.append(
            {"column": name, "pass": not failed_roots, "failed_roots": failed_roots}
        )

    return {"pass": all_pass, "columns": column_reports, "error_msg": ""}
