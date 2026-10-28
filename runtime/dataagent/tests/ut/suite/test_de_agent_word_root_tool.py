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
"""Unit tests for the identifier wrapper; no dictionary or LLM is needed."""

import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.de_agent.hooks.word_roots import (
    validate_identifier_word_roots,
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

    result = await validate_identifier_word_roots(["ads_user"], _tool_context=context)

    assert result["pass"] is False
    assert result["names"] == []
    assert error in result["error_msg"]
    validator.assert_awaited_once()


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
async def test_names_all_match_dedup_roots_and_omit_default_k(validator, context):
    # "user" appears in both names but must be validated only once.
    validator.side_effect = lambda *, items, _tool_context, **opts: _match_items(items)

    result = await validate_identifier_word_roots(["ads_user", "user_id"], _tool_context=context)

    assert result == {
        "pass": True,
        "names": [
            {"name": "ads_user", "pass": True, "failed_roots": []},
            {"name": "user_id", "pass": True, "failed_roots": []},
        ],
        "error_msg": "",
    }
    validator.assert_awaited_once_with(
        items=[{"english_name": "ads"}, {"english_name": "user"}, {"english_name": "id"}],
        _tool_context=context,
    )


@pytest.mark.asyncio
async def test_names_report_per_name_failures_with_in_name_index(validator, context):
    miss = {
        "found": False, "match_type": None,
        "candidates": [{"record": {"词根英文名": "user", "词根中文名": "用户"}, "relevance": 90}],
    }
    validator.side_effect = lambda *, items, _tool_context, **opts: _match_items(
        items, found_overrides={"usr": miss}
    )

    result = await validate_identifier_word_roots(
        ["ads_user", "usr_cnt"], k=10, llm_batch_size=2, _tool_context=context
    )

    assert result == {
        "pass": False,
        "names": [
            {"name": "ads_user", "pass": True, "failed_roots": []},
            {
                "name": "usr_cnt",
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
async def test_shared_failed_root_is_reported_at_every_position(validator, context):
    miss = {
        "found": False, "match_type": None,
        "candidates": [{"record": {"词根英文名": "user", "词根中文名": "用户"}, "relevance": 90}],
    }
    response = _match_items(
        [{"english_name": root} for root in ["ads", "usr", "id", "cnt"]],
        found_overrides={"usr": miss},
    )
    validator.return_value = response

    result = await validate_identifier_word_roots(
        ["ads_usr_usr", "usr_id", "cnt_usr", "id_cnt"], _tool_context=context
    )

    assert result == {
        "pass": False,
        "names": [
            {
                "name": "ads_usr_usr", "pass": False,
                "failed_roots": [
                    {"index": 1, "english_name": "usr", **miss},
                    {"index": 2, "english_name": "usr", **miss},
                ],
            },
            {
                "name": "usr_id", "pass": False,
                "failed_roots": [{"index": 0, "english_name": "usr", **miss}],
            },
            {
                "name": "cnt_usr", "pass": False,
                "failed_roots": [{"index": 1, "english_name": "usr", **miss}],
            },
            {"name": "id_cnt", "pass": True, "failed_roots": []},
        ],
        "error_msg": "",
    }
    validator.assert_awaited_once_with(
        items=[{"english_name": root} for root in ["ads", "usr", "id", "cnt"]],
        _tool_context=context,
    )
    # Reporting each occurrence must not mutate the shared source result.
    assert response["results"][1] == {"index": 1, "english_name": "usr", **miss}


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_root", [None, "c22"])
async def test_names_chunk_unique_roots_over_the_batch_limit(validator, context, failed_root):
    miss = {
        "found": False, "match_type": None,
        "candidates": [{"record": {"词根英文名": "cnt", "词根中文名": "数量"}, "relevance": 90}],
    }
    validator.side_effect = lambda *, items, _tool_context, **opts: _match_items(
        items, found_overrides={failed_root: miss} if failed_root else None
    )
    names = [f"c{n}" for n in range(25)]  # 25 unique single roots → chunks of 20 + 5

    result = await validate_identifier_word_roots(names, _tool_context=context)

    assert result == {
        "pass": failed_root is None,
        "names": [
            {
                "name": name,
                "pass": name != failed_root,
                # c22 has batch-local index 2, but index 0 within its name.
                "failed_roots": [{"index": 0, "english_name": name, **miss}]
                if name == failed_root else [],
            }
            for name in names
        ],
        "error_msg": "",
    }
    assert validator.await_count == 2
    calls = validator.await_args_list
    assert calls[0].kwargs["items"] == [{"english_name": name} for name in names[:20]]
    assert calls[1].kwargs["items"] == [{"english_name": name} for name in names[20:]]


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [[], "ads_user", None])
async def test_names_require_non_empty_list(validator, context, names):
    result = await validate_identifier_word_roots(names, _tool_context=context)

    assert result == {"pass": False, "names": [], "error_msg": "names 必须是非空列表。"}
    validator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["Bad", "ads__user", "a.b", "_ads", "ads_", "1ads", ""])
async def test_names_reject_invalid_identifiers(validator, context, bad):
    result = await validate_identifier_word_roots(["ads_user", bad], _tool_context=context)

    assert result["pass"] is False
    assert result["names"] == []
    assert "名称 1" in result["error_msg"]
    validator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, error", [
    ({"k": 0}, "k 必须"),
    ({"k": 21}, "k 必须"),
    ({"k": True}, "k 必须"),
    ({"llm_batch_size": 0}, "llm_batch_size 必须"),
    ({"llm_batch_size": "2"}, "llm_batch_size 必须"),
])
async def test_names_invalid_parameters_do_not_call_validator(validator, context, kwargs, error):
    result = await validate_identifier_word_roots(["ads_user"], **kwargs, _tool_context=context)

    assert result["pass"] is False
    assert result["names"] == []
    assert error in result["error_msg"]
    validator.assert_not_awaited()


@pytest.mark.asyncio
async def test_names_skipped_validation_degrades_cleanly(validator, context):
    validator.return_value = {
        "validation_status": "skipped",
        "reason": "word_root_csv_missing",
        "results": [{"index": 0, "english_name": "ads", "found": None, "match_type": None}],
    }

    result = await validate_identifier_word_roots(["ads_user"], _tool_context=context)

    assert result == {
        "pass": False, "names": [],
        "error_msg": "词根字典缺失，已跳过词根校验。",
    }
    validator.assert_awaited_once()


@pytest.mark.asyncio
async def test_names_service_exception_is_returned_as_error(validator, context):
    validator.side_effect = RuntimeError("service unavailable")

    result = await validate_identifier_word_roots(["ads_user"], _tool_context=context)

    assert result == {
        "pass": False, "names": [],
        "error_msg": "词根校验不可用：RuntimeError: service unavailable",
    }
    validator.assert_awaited_once()
