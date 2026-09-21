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
from typing import Any

import pytest

from dataagent.agents.nl2sql.nodes import business_twin_perceptor as business_twin_perceptor_module
from dataagent.agents.nl2sql.nodes.business_twin_perceptor import BusinessTwinPerceptorNode


class _ConfigManager:
    def get(self, key: str, default: Any = None) -> Any:
        return default


def _node(monkeypatch: pytest.MonkeyPatch, *, explicit_granularity: bool) -> BusinessTwinPerceptorNode:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())
    monkeypatch.setattr(node, "_load_prompt", lambda _name: "existing rule")
    monkeypatch.setattr(business_twin_perceptor_module, "schema_to_ddl", lambda *_args: "DDL")

    async def fake_schema_linking(_question: str):
        return {}, [], {}, explicit_granularity

    monkeypatch.setattr(node, "_business_twin_schema_linking", fake_schema_linking)
    return node


@pytest.mark.asyncio
async def test_named_granularity_makes_time_mandatory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The selector already tells a named granularity apart, so downstream nodes need not re-read it."""
    node = _node(monkeypatch, explicit_granularity=True)

    result = await node._aprocess({"question": "查询最近一周按全省统计的粒度为1h的MOS分段1次数"})

    rules = result["sql_rules"]
    assert "\n\n## 时间粒度" in rules
    assert "`time` 必须出现在 SELECT 和 GROUP BY" in rules
    assert "不得把结果压成单行" in rules
    assert "任何要求移除 `time` 的意见都应判为无效" in rules


@pytest.mark.asyncio
async def test_derived_granularity_leaves_time_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    """A granularity the selector derived only picks the table; it must not force a per-bucket output."""
    node = _node(monkeypatch, explicit_granularity=False)

    result = await node._aprocess({"question": "查询最近一周按全省统计的保障失败原因次数"})

    assert "## 时间粒度" not in result["sql_rules"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"family_name": "fam", "granularity": "1h", "explicit_granularity": True}, True),
        ({"family_name": "fam", "granularity": "1h", "explicit_granularity": False}, False),
        ({"family_name": "fam", "granularity": "1h"}, False),
    ],
    ids=["named", "derived", "field-omitted"],
)
@pytest.mark.asyncio
async def test_selector_reports_whether_the_question_named_the_granularity(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], expected: bool
) -> None:
    """A missing or non-true value must read as derived rather than forcing `time`."""
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fake_llm_json(_context: dict[str, str], action: str = "") -> Any:
        return payload

    monkeypatch.setattr(node, "execute_with_llm_json", fake_llm_json)
    monkeypatch.setattr(node, "_format_table_family_prompt_context", lambda _families: "")

    selection = await node._select_table_family("question", [{"family_name": "fam"}])

    assert selection is not None
    assert selection["explicit_granularity"] is expected
