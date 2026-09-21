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
from __future__ import annotations

from typing import Any

import pytest

from dataagent.agents.nl2sql.errors import NL2SQLError
from dataagent.agents.nl2sql.nodes.business_twin_perceptor import BusinessTwinPerceptorNode

EXPLICIT_ONLY_TABLE = "fact_dw1745159004_0000000000000000_metric_1h"
EXCLUDED_FAMILY = "fact_dw1745159004_0000000000000000"
OTHER_TABLE = "fact_dw1745159004_0000000000003000_metric_1h"
OTHER_FAMILY = "fact_dw1745159004_0000000000003000"
ABSENT_TABLE = "fact_dw1745159099_000000000000000f_metric_1d"


class _ConfigManager:
    def get(self, _key: str, default: Any = None) -> Any:
        return default


def _catalog() -> list[dict[str, str]]:
    return [
        {
            "bare_table_name": EXPLICIT_ONLY_TABLE,
            "business_id": "dw1745159004",
            "dimension_code": "0000000000000000",
            "granularity": "1h",
        },
        {
            "bare_table_name": OTHER_TABLE,
            "business_id": "dw1745159004",
            "dimension_code": "0000000000003000",
            "granularity": "1h",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (f"在{EXPLICIT_ONLY_TABLE}表中，查询task_id为XXXX的保障时长", EXPLICIT_ONLY_TABLE),
        (f"在{OTHER_TABLE}表中，查询保障时长", OTHER_TABLE),
        (f"select * from {OTHER_TABLE} where task_id = 'XXXX'", OTHER_TABLE),
        (f"查询{OTHER_TABLE.upper()}中task_id为XXXX的保障时长", OTHER_TABLE),
        (f"在db.schema.{OTHER_TABLE}表中查询保障时长", OTHER_TABLE),
    ],
)
async def test_mentioned_table_is_pinned_without_llm(
    monkeypatch: pytest.MonkeyPatch, question: str, expected: str
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())

    async def fail_execute(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("pinned question must not invoke any selection LLM call")

    monkeypatch.setattr(node, "execute_with_llm_json", fail_execute)
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _catalog())

    # Pinning names a table, not an output granularity, so no granularity is claimed.
    assert await node._select_table_by_business_family(question) == (expected, False)


@pytest.mark.asyncio
async def test_mentioned_table_not_in_catalog_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _catalog())

    with pytest.raises(NL2SQLError, match="not in the catalog"):
        await node._select_table_by_business_family(f"在{ABSENT_TABLE}表中查询保障时长")


@pytest.mark.asyncio
async def test_multiple_mentioned_tables_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _catalog())

    with pytest.raises(NL2SQLError, match="multiple tables"):
        await node._select_table_by_business_family(f"对比{EXPLICIT_ONLY_TABLE}和{OTHER_TABLE}的保障时长")


@pytest.mark.asyncio
async def test_unmentioned_question_excludes_explicit_only_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())
    seen_families: list[list[dict[str, Any]]] = []

    async def fake_business_id(question: str) -> str:
        return "dw1745159004"

    async def fake_select_family(question: str, families: list[dict[str, Any]]) -> dict[str, Any]:
        seen_families.append(families)
        return {"family_name": OTHER_FAMILY, "granularity": "1h", "explicit_granularity": True}

    monkeypatch.setattr(node, "_select_business_id", fake_business_id)
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _catalog())
    monkeypatch.setattr(node, "_select_table_family", fake_select_family)

    question = "查询最近一周按全省（不区分城市）统计的无线小区下行PRB可用数，该指标对应字段为cell_prb_dl_total，单位为个，按1小时粒度展示"
    table, explicit_granularity = await node._select_table_by_business_family(question)

    assert table == OTHER_TABLE
    assert explicit_granularity is True
    assert len(seen_families) == 1
    family_names = {family["family_name"] for family in seen_families[0]}
    assert EXCLUDED_FAMILY not in family_names
    assert OTHER_FAMILY in family_names


@pytest.mark.asyncio
async def test_plain_question_keeps_normal_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = BusinessTwinPerceptorNode(config_manager=_ConfigManager())
    called = {"business_id": False}

    async def fake_business_id(question: str) -> str:
        called["business_id"] = True
        return "dw1745159004"

    async def fake_select_family(question: str, families: list[dict[str, Any]]) -> dict[str, Any]:
        return {"family_name": OTHER_FAMILY, "granularity": "1h", "explicit_granularity": True}

    monkeypatch.setattr(node, "_select_business_id", fake_business_id)
    monkeypatch.setattr(node, "_full_table_catalog", lambda: _catalog())
    monkeypatch.setattr(node, "_select_table_family", fake_select_family)

    question = "查询最近一周全省无线小区下行PRB可用数，按1小时粒度展示"
    assert await node._select_table_by_business_family(question) == (OTHER_TABLE, True)
    assert called["business_id"] is True


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (f"在{EXPLICIT_ONLY_TABLE}表中查询保障时长", [EXPLICIT_ONLY_TABLE]),
        (f"对比{EXPLICIT_ONLY_TABLE}和{OTHER_TABLE}的保障时长", [EXPLICIT_ONLY_TABLE, OTHER_TABLE]),
        (f"在db.schema.{OTHER_TABLE}表中查询保障时长", [OTHER_TABLE]),
        (f"prefix_{OTHER_TABLE}", []),  # 前缀粘连的更长表名不算点名
        (f"{OTHER_TABLE}_suffix", []),  # 后缀粘连的更长表名不算点名
        ("查询无线小区下行PRB可用数", []),
        ("在fact_dw1745159004_0000000000003000_metric_1min表中查询保障时长", []),  # 不存在的粒度
    ],
)
def test_mention_detection_boundaries(question: str, expected: list[str]) -> None:
    assert BusinessTwinPerceptorNode._mentioned_tables(question) == expected
