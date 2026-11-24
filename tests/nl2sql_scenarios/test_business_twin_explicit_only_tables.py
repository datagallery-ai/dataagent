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
"""Explicit-only table elimination in the business_twin selection pipeline.

Ported from the legacy
``tests/ut/agents/nl2sql/test_business_twin_explicit_only_tables.py``
(br_release_930): one catalog table is reachable only by naming it. Questions
that mention a table pin it directly with no LLM call, while the
business-family chain never offers the explicit-only table as a candidate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from nl2sql.errors import DataAgentError
from scenarios.udn.business_twin import table_selector as table_selector_module
from scenarios.udn.business_twin.table_selector import BusinessTwinTableSelector

from nl2sql_scenarios.support import ScriptedModel, make_context, scripted_model, semantic_client

PACK_DIR = Path(table_selector_module.__file__).resolve().parent
SEMANTIC_SCENARIO = "business_twin"

EXPLICIT_ONLY_TABLE = "fact_dw1745159004_0000000000000000_metric_1h"
EXCLUDED_FAMILY = "fact_dw1745159004_0000000000000000"
OTHER_TABLE = "fact_dw1745159004_0000000000003000_metric_1h"
OTHER_FAMILY = "fact_dw1745159004_0000000000003000"
ABSENT_TABLE = "fact_dw1745159099_000000000000000f_metric_1d"


def _column_extraction(columns: list[str]) -> str:
    """Scripted LLM reply: the extracted canonical columns as a bare JSON array."""
    return f"```json\n{json.dumps(columns)}\n```"


def _family_selection(family: str) -> str:
    """Scripted LLM reply: the model's table-family pick with explicit granularity."""
    payload = json.dumps({"family_name": family, "granularity": "1h", "explicit_granularity": True})
    return f"```json\n{payload}\n```"


def table_list_transport(
    tables: list[str],
) -> Callable[[httpx.Request], httpx.Response]:
    """Semantic-service mock serving only the table-list endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Semantic-Scenario") == SEMANTIC_SCENARIO, dict(
            request.headers
        )
        assert request.url.path.endswith("/advanced-search/table-list"), request.url.path
        return httpx.Response(
            200,
            json=[{f"business_twin.{name}": {"table_description": ""}} for name in tables],
        )

    return handler


async def select_table(model: ScriptedModel, tables: list[str], question: str) -> tuple[str, bool]:
    """Drive the selection pipeline: a selector wired like ``build_scenario`` over a mock catalog."""
    with semantic_client(table_list_transport(tables), scenario=SEMANTIC_SCENARIO) as client:
        selector = BusinessTwinTableSelector(
            make_context("udn.business_twin", model, client),
            base=PACK_DIR,
            semantic_client=client,
        )
        return await selector._select_table_by_business_family(question)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        pytest.param(
            f"在{EXPLICIT_ONLY_TABLE}表中，查询task_id为XXXX的保障时长",
            EXPLICIT_ONLY_TABLE,
            id="chinese",
        ),
        pytest.param(f"在{OTHER_TABLE}表中，查询保障时长", OTHER_TABLE, id="plain"),
        pytest.param(f"select * from {OTHER_TABLE} where task_id = 'XXXX'", OTHER_TABLE, id="sql"),
        pytest.param(
            f"查询{OTHER_TABLE.upper()}中task_id为XXXX的保障时长", OTHER_TABLE, id="uppercase"
        ),
        pytest.param(f"在db.schema.{OTHER_TABLE}表中查询保障时长", OTHER_TABLE, id="db-qualified"),
    ],
)
async def test_mentioned_table_is_pinned_without_llm(question: str, expected: str) -> None:
    model = scripted_model()  # any LLM call would exhaust the stub and fail the test

    # Pinning names a table, not an output granularity, so no granularity is claimed.
    assert await select_table(model, [EXPLICIT_ONLY_TABLE, OTHER_TABLE], question) == (
        expected,
        False,
    )
    assert model.requests == []


async def test_mentioned_table_not_in_catalog_raises() -> None:
    with pytest.raises(DataAgentError, match="not in the catalog"):
        await select_table(
            scripted_model(),
            [EXPLICIT_ONLY_TABLE, OTHER_TABLE],
            f"在{ABSENT_TABLE}表中查询保障时长",
        )


async def test_multiple_mentioned_tables_raise() -> None:
    with pytest.raises(DataAgentError, match="multiple tables"):
        await select_table(
            scripted_model(),
            [EXPLICIT_ONLY_TABLE, OTHER_TABLE],
            f"对比{EXPLICIT_ONLY_TABLE}和{OTHER_TABLE}的保障时长",
        )


async def test_unmentioned_question_excludes_explicit_only_table() -> None:
    """The explicit-only table's family never reaches the family-selection prompt."""
    question = (
        "查询最近一周按全省（不区分城市）统计的无线小区下行PRB可用数，"
        "该指标对应字段为cell_prb_dl_total，单位为个，按1小时粒度展示"
    )
    model = scripted_model(
        _column_extraction(["cell_prb_dl_total", "1h_granularity"]),
        _family_selection(OTHER_FAMILY),
    )

    assert await select_table(model, [EXPLICIT_ONLY_TABLE, OTHER_TABLE], question) == (
        OTHER_TABLE,
        True,
    )
    assert len(model.requests) == 2  # column extraction + family selection
    family_prompt = model.requests[1][-1].content
    assert EXCLUDED_FAMILY not in family_prompt
    assert OTHER_FAMILY in family_prompt


async def test_plain_question_keeps_normal_flow() -> None:
    """A question naming no table runs the full business-ID to family chain."""
    question = "查询最近一周全省无线小区下行PRB可用数，按1小时粒度展示"
    model = scripted_model(
        _column_extraction(["cell_prb_dl_total", "1h_granularity"]),
        _family_selection(OTHER_FAMILY),
    )

    assert await select_table(model, [EXPLICIT_ONLY_TABLE, OTHER_TABLE], question) == (
        OTHER_TABLE,
        True,
    )
    assert len(model.requests) == 2  # the business-ID extraction ran before family selection


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        pytest.param(
            f"在{EXPLICIT_ONLY_TABLE}表中查询保障时长", [EXPLICIT_ONLY_TABLE], id="single"
        ),
        pytest.param(
            f"对比{EXPLICIT_ONLY_TABLE}和{OTHER_TABLE}的保障时长",
            [EXPLICIT_ONLY_TABLE, OTHER_TABLE],
            id="multiple",
        ),
        pytest.param(
            f"在db.schema.{OTHER_TABLE}表中查询保障时长", [OTHER_TABLE], id="db-qualified"
        ),
        # A longer name glued to the table name is not a mention.
        pytest.param(f"prefix_{OTHER_TABLE}", [], id="glued-prefix"),
        pytest.param(f"{OTHER_TABLE}_suffix", [], id="glued-suffix"),
        pytest.param("查询无线小区下行PRB可用数", [], id="no-mention"),
        pytest.param(
            "在fact_dw1745159004_0000000000003000_metric_1min表中查询保障时长",
            [],
            id="unknown-granularity",
        ),
    ],
)
def test_mention_detection_boundaries(question: str, expected: list[str]) -> None:
    assert BusinessTwinTableSelector._mentioned_tables(question) == expected
