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
"""Local match coverage for word-root CSV validation (no LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool import word_root_validator as wrv


CSV_HEADER = (
    "词根英文名,词根中文名,描述,词根类型,特殊词根规则,词根简称\n"
)


def _write_csv(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "word_roots.csv"
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    return path


def _context(csv_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_config={
            "csv_path": str(csv_path),
            "english_name_column": "词根英文名",
            "chinese_name_column": "词根中文名",
            "description_column": "描述",
            "type_column": "词根类型",
            "regex_column": "特殊词根规则",
            "abbreviation_column": "词根简称",
            "special_type_value": "特殊词根",
            "llm_model": "planner",
        }
    )


@pytest.fixture(autouse=True)
def _clear_caches():
    wrv._load_word_root_table_cached.cache_clear()
    with wrv._result_cache_lock:
        wrv._result_cache.clear()
    yield
    wrv._load_word_root_table_cached.cache_clear()
    with wrv._result_cache_lock:
        wrv._result_cache.clear()


@pytest.mark.asyncio
async def test_abbreviation_match_counts_as_found(tmp_path: Path):
    csv_path = _write_csv(
        tmp_path,
        "application,应用,应用主体,普通词根,,app\n"
        "user,用户,用户主体,普通词根,,\n",
    )

    result = await wrv.validate_word_roots(
        items=[{"english_name": "app"}, {"english_name": "user"}],
        _tool_context=_context(csv_path),
    )

    assert result["results"][0]["found"] is True
    assert result["results"][0]["match_type"] == "abbreviation"
    assert result["results"][0]["matches"][0]["词根英文名"] == "application"
    assert result["results"][1]["found"] is True
    assert result["results"][1]["match_type"] == "exact"


@pytest.mark.asyncio
async def test_english_name_wins_over_abbreviation(tmp_path: Path):
    csv_path = _write_csv(
        tmp_path,
        "app,应用缩写条目,desc,普通词根,,\n"
        "application,应用,desc,普通词根,,app\n",
    )

    result = await wrv.validate_word_root("app", _tool_context=_context(csv_path))

    assert result["found"] is True
    assert result["match_type"] == "exact"
    assert result["matches"][0]["词根英文名"] == "app"


def test_empty_abbreviation_does_not_match(tmp_path: Path):
    csv_path = _write_csv(
        tmp_path,
        "application,应用,desc,普通词根,,\n",
    )
    table = wrv._load_word_root_table(_context(csv_path).tool_config)

    assert wrv._local_match_result(table, "app") is None
    assert wrv._local_match_result(table, "application")["match_type"] == "exact"
