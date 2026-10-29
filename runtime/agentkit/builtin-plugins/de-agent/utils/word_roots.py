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
"""Split, deduplicate and validate table/column roots using the original contracts."""

import copy
import re
from typing import Any

from utils.word_root_validator import WordRootContext, validate_word_roots

# A single lowercase identifier segment: starts with a letter, no leading/trailing
# or consecutive underscores.
_IDENTIFIER = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
# Mirror the underlying validator's _MAX_BATCH_QUERY_ITEMS: a single batch accepts
# at most this many unique roots, so identifier roots are chunked to stay within it.
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
    _tool_context: WordRootContext,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Validate one batch of roots against the word-root dictionary.

    Returns (results, "") on success, where results is aligned one-to-one with
    ``roots``. On any failure, including a skipped validation when the dictionary
    is missing, returns (None, error_msg); the message is conclusive and the
    caller must not treat the roots as passed.
    """
    try:
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


async def validate_identifier_word_roots(
    names: list[str],
    k: int | None = None,
    llm_batch_size: int | None = None,
    *,
    _tool_context: WordRootContext,
) -> dict[str, Any]:
    """Validate word roots of table and column names.

    Args:
        names: Non-empty list of bare table or column names, without database
            prefixes. Use lowercase letters, digits and underscores, starting
            with a letter, without leading/trailing or consecutive underscores.
        k: Maximum candidate count per unmatched root, between 1 and 20.
            Omit or pass None to use the underlying validator's default.
        llm_batch_size: Number of unmatched roots per LLM call. Optional positive
            integer; omit or pass None to use the underlying validator's default.

    Returns:
        pass: True only when every root has an exact or regex match.
        names: Results in input order, each with name, pass and failed_roots.
            Failed root indices refer to positions within that name.
        error_msg: Parameter, service or response error; empty after a completed
            validation. On error, names is empty and not conclusive.
    """
    if not isinstance(names, list) or not names:
        return {"pass": False, "names": [], "error_msg": "names 必须是非空列表。"}
    param_error = _check_common_params(k, llm_batch_size)
    if param_error:
        return {"pass": False, "names": [], "error_msg": param_error}

    identifier_roots: list[tuple[str, list[str]]] = []
    for index, name_value in enumerate(names):
        name = name_value.strip() if isinstance(name_value, str) else ""
        if not re.fullmatch(_IDENTIFIER, name):
            return {
                "pass": False,
                "names": [],
                "error_msg": (
                    f"名称 {index} ({name_value!r}) 非法：应使用小写字母、数字和下划线，"
                    "以字母开头，不允许连续或首尾下划线。"
                ),
            }
        identifier_roots.append((name, name.split("_")))

    # Deduplicate roots across all names, preserving first-seen order.
    unique_roots: list[str] = []
    seen: set[str] = set()
    for _, roots in identifier_roots:
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
            return {"pass": False, "names": [], "error_msg": error_msg}
        for root, result in zip(chunk, results):
            root_result[root] = result

    reports: list[dict[str, Any]] = []
    all_pass = True
    for name, roots in identifier_roots:
        failed_roots: list[dict[str, Any]] = []
        for position, root in enumerate(roots):
            result = root_result[root]
            if not result["found"]:
                # Copy the shared root result so each identifier can carry its own
                # identifier-specific position without mutating the deduplicated original.
                item = copy.deepcopy(result)
                item["index"] = position
                failed_roots.append(item)
        if failed_roots:
            all_pass = False
        reports.append(
            {"name": name, "pass": not failed_roots, "failed_roots": failed_roots}
        )

    return {"pass": all_pass, "names": reports, "error_msg": ""}
