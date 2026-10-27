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
"""Unit tests for the table-name wrapper; no dictionary or LLM is needed."""

import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.de_agent.tools.table_word_roots import (
    validate_column_word_roots,
    validate_table_word_roots,
)


@pytest.fixture
def validator(monkeypatch):
    # The underlying module may be absent locally. Replace only that dependency;
    # the wrapper and ToolExecutionContext are imported from the real package.
    module_name = "dataagent.actions.tools.local_tool.word_root_validator"
    module = ModuleType(module_name)
    mock = AsyncMock()
    module.validate_word_roots = mock
    monkeypatch.setitem(sys.modules, module_name, module)
    return mock


@pytest.fixture
def context():
    return ToolExecutionContext(tool_config={"llm_model": "planner"})


def matched(index, name, match_type="exact"):
    return {"index": index, "english_name": name, "found": True, "match_type": match_type}


@pytest.mark.asyncio
@pytest.mark.parametrize("table_name", ["ads_user_user_30d", " db.ads_user_user_30d "])
async def test_all_roots_match_and_default_batch_size_is_omitted(validator, context, table_name):
    roots = ["ads", "user", "user", "30d"]
    validator.return_value = {"results": [
        matched(i, root, "regex" if root == "30d" else "exact") for i, root in enumerate(roots)
    ]}

    result = await validate_table_word_roots(table_name, _tool_context=context)

    assert result == {"pass": True, "failed_roots": [], "error_msg": ""}
    validator.assert_awaited_once_with(
        items=[{"english_name": root} for root in roots], _tool_context=context
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [None, 2])
async def test_unmatched_results_preserve_candidates_and_forward_options(validator, context, batch_size):
    miss = {
        "index": 1, "english_name": "usr", "found": False, "match_type": None,
        "candidates": [{"record": {"词根英文名": "user", "词根中文名": "用户"}, "relevance": 95}],
    }
    validator.return_value = {"results": [matched(0, "ads"), miss]}

    result = await validate_table_word_roots(
        "db.ads_usr", k=10, llm_batch_size=batch_size, _tool_context=context
    )

    assert result == {"pass": False, "failed_roots": [miss], "error_msg": ""}
    options = {} if batch_size is None else {"llm_batch_size": batch_size}
    validator.assert_awaited_once_with(
        items=[{"english_name": "ads"}, {"english_name": "usr"}],
        k=10, _tool_context=context, **options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, error", [
    ({"table_name": ""}, "表名应为"),
    ({"table_name": "   "}, "表名应为"),
    ({"table_name": "ads__user"}, "表名应为"),
    ({"table_name": "a.b.c"}, "表名应为"),
    ({"k": 0}, "k 必须"),
    ({"k": 21}, "k 必须"),
    ({"k": True}, "k 必须"),
    ({"llm_batch_size": 0}, "llm_batch_size 必须"),
    ({"llm_batch_size": "2"}, "llm_batch_size 必须"),
])
async def test_invalid_parameters_do_not_call_validator(validator, context, kwargs, error):
    result = await validate_table_word_roots(
        **{"table_name": "ads_user", **kwargs}, _tool_context=context
    )

    assert result["pass"] is False
    assert result["failed_roots"] == []
    assert error in result["error_msg"]
    validator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response, error", [
    ({}, "results 数组"),
    ({"results": None}, "results 数组"),
    ({"results": []}, "数量不一致"),
    ({"results": [matched(0, "ads"), matched(1, "user"), matched(2, "extra")]}, "数量不一致"),
    ({"results": [matched(1, "user"), matched(0, "ads")]}, "索引"),
    ({"results": [matched(0, "ads"), matched(0, "user")]}, "索引"),
    ({"results": [matched(0, "ads"), matched(1, "other")]}, "英文名"),
    ({"results": [matched(0, "ads"), None]}, "返回结果异常"),
    ({"results": [matched(0, "ads"), {"index": 1, "english_name": "user", "found": "true"}]}, "found"),
    ({"results": [matched(0, "ads"), matched(1, "user", "unknown")]}, "match_type"),
])
async def test_malformed_response_never_passes(validator, context, response, error):
    validator.return_value = response

    result = await validate_table_word_roots("ads_user", _tool_context=context)

    assert result["pass"] is False
    assert result["failed_roots"] == []
    assert error in result["error_msg"]
    validator.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_exception_is_returned_as_error(validator, context):
    validator.side_effect = RuntimeError("service unavailable")

    result = await validate_table_word_roots("ads_user", _tool_context=context)

    assert result == {
        "pass": False, "failed_roots": [],
        "error_msg": "词根校验不可用：RuntimeError: service unavailable",
    }
    validator.assert_awaited_once()


@pytest.mark.asyncio
async def test_table_skipped_validation_degrades_cleanly(validator, context):
    # Missing dictionary: the underlying tool returns skipped with found=None.
    # The wrapper must report a degraded message, not a "found 异常" error.
    validator.return_value = {
        "validation_status": "skipped",
        "reason": "word_root_csv_missing",
        "results": [
            {"index": 0, "english_name": "ads", "found": None, "match_type": None},
            {"index": 1, "english_name": "user", "found": None, "match_type": None},
        ],
    }

    result = await validate_table_word_roots("ads_user", _tool_context=context)

    assert result == {
        "pass": False, "failed_roots": [],
        "error_msg": "词根字典缺失，已跳过词根校验。",
    }
    validator.assert_awaited_once()


# --- validate_column_word_roots -------------------------------------------------


def _match_items(items, found_overrides=None):
    # Build a results array aligned with the items a single batch call received.
    found_overrides = found_overrides or {}
    results = []
    for i, item in enumerate(items):
        name = item["english_name"]
        if name in found_overrides:
            results.append({"index": i, "english_name": name, **found_overrides[name]})
        else:
            results.append({"index": i, "english_name": name, "found": True, "match_type": "exact"})
    return {"results": results}


@pytest.mark.asyncio
async def test_columns_all_match_dedup_roots_and_omit_default_k(validator, context):
    # "user" appears in both columns but must be validated only once.
    validator.side_effect = lambda *, items, _tool_context, **opts: _match_items(items)

    result = await validate_column_word_roots(["ads_user", "user_id"], _tool_context=context)

    assert result == {
        "pass": True,
        "columns": [
            {"column": "ads_user", "pass": True, "failed_roots": []},
            {"column": "user_id", "pass": True, "failed_roots": []},
        ],
        "error_msg": "",
    }
    validator.assert_awaited_once_with(
        items=[{"english_name": "ads"}, {"english_name": "user"}, {"english_name": "id"}],
        _tool_context=context,
    )


@pytest.mark.asyncio
async def test_columns_report_per_column_failures_with_in_column_index(validator, context):
    miss = {
        "found": False, "match_type": None,
        "candidates": [{"record": {"词根英文名": "user", "词根中文名": "用户"}, "relevance": 90}],
    }
    validator.side_effect = lambda *, items, _tool_context, **opts: _match_items(
        items, found_overrides={"usr": miss}
    )

    result = await validate_column_word_roots(
        ["ads_user", "usr_cnt"], k=10, llm_batch_size=2, _tool_context=context
    )

    assert result == {
        "pass": False,
        "columns": [
            {"column": "ads_user", "pass": True, "failed_roots": []},
            {
                "column": "usr_cnt",
                "pass": False,
                # index rewritten to the root's position inside "usr_cnt".
                "failed_roots": [{"index": 0, "english_name": "usr", **miss}],
            },
        ],
        "error_msg": "",
    }
    validator.assert_awaited_once_with(
        items=[
            {"english_name": "ads"}, {"english_name": "user"},
            {"english_name": "usr"}, {"english_name": "cnt"},
        ],
        k=10, llm_batch_size=2, _tool_context=context,
    )


@pytest.mark.asyncio
async def test_columns_chunk_unique_roots_over_the_batch_limit(validator, context):
    validator.side_effect = lambda *, items, _tool_context, **opts: _match_items(items)
    columns = [f"c{n}" for n in range(25)]  # 25 unique single roots → chunks of 20 + 5

    result = await validate_column_word_roots(columns, _tool_context=context)

    assert result["pass"] is True
    assert [c["column"] for c in result["columns"]] == columns
    assert validator.await_count == 2
    calls = validator.await_args_list
    assert len(calls[0].kwargs["items"]) == 20
    assert len(calls[1].kwargs["items"]) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("columns", [[], "ads_user", None])
async def test_columns_require_non_empty_list(validator, context, columns):
    result = await validate_column_word_roots(columns, _tool_context=context)

    assert result == {"pass": False, "columns": [], "error_msg": "columns 必须是非空列表。"}
    validator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["Bad", "ads__user", "a.b", "_ads", "ads_", "1ads", ""])
async def test_columns_reject_invalid_identifiers(validator, context, bad):
    result = await validate_column_word_roots(["ads_user", bad], _tool_context=context)

    assert result["pass"] is False
    assert result["columns"] == []
    assert "列名 1" in result["error_msg"]
    validator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, error", [
    ({"k": 0}, "k 必须"),
    ({"k": 21}, "k 必须"),
    ({"k": True}, "k 必须"),
    ({"llm_batch_size": 0}, "llm_batch_size 必须"),
    ({"llm_batch_size": "2"}, "llm_batch_size 必须"),
])
async def test_columns_invalid_parameters_do_not_call_validator(validator, context, kwargs, error):
    result = await validate_column_word_roots(["ads_user"], **kwargs, _tool_context=context)

    assert result["pass"] is False
    assert result["columns"] == []
    assert error in result["error_msg"]
    validator.assert_not_awaited()


@pytest.mark.asyncio
async def test_columns_skipped_validation_degrades_cleanly(validator, context):
    validator.return_value = {
        "validation_status": "skipped",
        "reason": "word_root_csv_missing",
        "results": [{"index": 0, "english_name": "ads", "found": None, "match_type": None}],
    }

    result = await validate_column_word_roots(["ads_user"], _tool_context=context)

    assert result == {
        "pass": False, "columns": [],
        "error_msg": "词根字典缺失，已跳过词根校验。",
    }
    validator.assert_awaited_once()


@pytest.mark.asyncio
async def test_columns_service_exception_is_returned_as_error(validator, context):
    validator.side_effect = RuntimeError("service unavailable")

    result = await validate_column_word_roots(["ads_user"], _tool_context=context)

    assert result == {
        "pass": False, "columns": [],
        "error_msg": "词根校验不可用：RuntimeError: service unavailable",
    }
    validator.assert_awaited_once()
