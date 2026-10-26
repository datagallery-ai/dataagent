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
"""Validate all word roots in a proposed table name before writing DDL."""

import re
from typing import Any

from dataagent.actions.tools.context import ToolExecutionContext


async def validate_table_word_roots(
    table_name: str,
    k: int = 5,
    llm_batch_size: int | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """Validate a complete candidate table name against the word-root dictionary.

    Args:
        table_name: Lowercase table name, optionally qualified as db.table.
            Only the table part is split on underscores.
        k: Maximum candidate count per unmatched root, between 1 and 20.
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
    identifier = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
    if not re.fullmatch(rf"(?:{identifier}\.)?{identifier}", name):
        return {
            "pass": False,
            "failed_roots": [],
            "error_msg": (
                "表名应为 table 或 db.table，使用小写字母、数字和下划线，"
                "标识符以字母开头，不允许连续或首尾下划线。"
            ),
        }
    if type(k) is not int or not 1 <= k <= 20:
        return {"pass": False, "failed_roots": [], "error_msg": "k 必须是 1 到 20 之间的整数。"}
    if llm_batch_size is not None and (type(llm_batch_size) is not int or llm_batch_size <= 0):
        return {"pass": False, "failed_roots": [], "error_msg": "llm_batch_size 必须是正整数或 None。"}

    roots = name.rsplit(".", 1)[-1].split("_")
    try:
        # Keep the underlying tool's configuration and runtime. Only override
        # its batch size when explicitly supplied.
        from dataagent.actions.tools.local_tool.word_root_validator import validate_word_roots

        options = {} if llm_batch_size is None else {"llm_batch_size": llm_batch_size}
        response = await validate_word_roots(
            items=[{"english_name": root} for root in roots],
            k=k,
            _tool_context=_tool_context,
            **options,
        )
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            return {"pass": False, "failed_roots": [], "error_msg": "词根接口未返回 results 数组。"}
    except Exception as exc:
        return {
            "pass": False,
            "failed_roots": [],
            "error_msg": f"词根校验不可用：{type(exc).__name__}: {exc}",
        }

    if len(roots) != len(results):
        return {
            "pass": False,
            "failed_roots": [],
            "error_msg": f"词根校验结果数量不一致：期望 {len(roots)} 项，实际 {len(results)} 项。",
        }

    failed_roots = []
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
            return {
                "pass": False,
                "failed_roots": [],
                "error_msg": f"词根 {index} ({root})：{error_msg}",
            }
        if result["found"]:
            continue
        failed_roots.append(result)

    return {"pass": not failed_roots, "failed_roots": failed_roots, "error_msg": ""}
