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
"""Time-granularity rule injection: the shared helper and both packs' selectors.

Merged port of the two legacy per-scenario test files on ``br_release_930``
(``test_business_twin_time_granularity_rule.py`` and
``test_traffic_insight_time_granularity_rule.py``). The old perceptors each
carried a verbatim copy of the ``## 时间粒度`` block; the packs now share
:mod:`scenarios.udn.common.rules`, so the text is pinned once here and each
selector's injection path is driven through ``select()`` with only the
schema-linking step stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from nl2sql.hooks import SchemaContext
from scenarios.udn.business_twin.table_selector import BusinessTwinTableSelector
from scenarios.udn.common.rules import current_date_rule, time_granularity_rules
from scenarios.udn.traffic_insight.table_selector import TrafficInsightTableSelector

from nl2sql_scenarios.support import make_context, scripted_model

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "builtin-plugins" / "nl2sql"
BT_BASE = PLUGIN_ROOT / "scenarios" / "udn" / "business_twin"
TI_BASE = PLUGIN_ROOT / "scenarios" / "udn" / "traffic_insight"

# The questions the legacy tests used: the first names a granularity, the second does not.
BT_NAMED_QUESTION = "查询最近一周按全省统计的粒度为1h的MOS分段1次数"
BT_DERIVED_QUESTION = "查询最近一周按全省统计的保障失败原因次数"
TI_NAMED_QUESTION = "查询最近一周粒度为1h的下行流量"
TI_DERIVED_QUESTION = "查询最近一周的下行流量"

# Minimal candidate families, shaped the way each pack's prompt formatter renders them.
BT_FAMILIES = [
    {
        "family_name": "fam",
        "dimensions": [],
        "tables_by_granularity": {"1h": "fact_dw1_0000000000000000_metric_1h"},
    }
]
TI_FAMILIES = [
    {
        "family_name": "fam",
        "dimensions": [],
        "metrics": [],
        "available_granularities": ["1h"],
    }
]


def _fenced_json(payload: dict[str, Any]) -> str:
    """Wrap a payload as the fenced JSON block the packs' model-output parser expects."""
    return f"```json\n{json.dumps(payload)}\n```"


# --- The merged helper: one text, pinned once ---------------------------------


def test_time_granularity_rules_text() -> None:
    """The merged block keeps the exact contract the old per-scenario copies pinned."""
    rules = time_granularity_rules()

    assert rules.startswith("\n\n## 时间粒度")
    assert "`time` 必须出现在 SELECT 和 GROUP BY" in rules
    assert "不得把结果压成单行" in rules
    assert "任何要求移除 `time` 的意见都应判为无效" in rules


def test_current_date_rule_relative_time_hint() -> None:
    """Only business_twin appends the relative-time parsing hint after today's date."""
    bare = current_date_rule()
    hinted = current_date_rule(relative_time_hint=True)

    assert "现在为" in bare
    assert "请基于当前日期解析用户问题中的相对时间" not in bare
    assert "请基于当前日期解析用户问题中的相对时间" in hinted


# --- business_twin injection ---------------------------------------------------


async def _business_twin_context(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    *,
    explicit_granularity: bool,
) -> SchemaContext:
    """Run business_twin ``select()`` with schema linking stubbed to one granularity verdict."""
    selector = BusinessTwinTableSelector(
        make_context("udn.business_twin", scripted_model(), None),
        base=BT_BASE,
        semantic_client=None,
    )

    async def fake_schema_linking(_question: str):
        return {}, [], {}, explicit_granularity

    monkeypatch.setattr(selector, "_schema_linking", fake_schema_linking)
    return await selector.select(question)


async def test_business_twin_named_granularity_makes_time_mandatory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selector already tells a named granularity apart; nodes need not re-read it."""
    context = await _business_twin_context(
        monkeypatch, BT_NAMED_QUESTION, explicit_granularity=True
    )

    rules = context.extra_rules
    assert "\n\n## 时间粒度" in rules
    assert "`time` 必须出现在 SELECT 和 GROUP BY" in rules
    assert "不得把结果压成单行" in rules
    assert "任何要求移除 `time` 的意见都应判为无效" in rules
    # business_twin (alone) also teaches the generator to anchor relative time on today's date.
    assert "现在为" in rules
    assert "请基于当前日期解析用户问题中的相对时间" in rules


async def test_business_twin_derived_granularity_leaves_time_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A derived granularity only picks the table; it must not force a per-bucket output."""
    context = await _business_twin_context(
        monkeypatch, BT_DERIVED_QUESTION, explicit_granularity=False
    )

    assert "## 时间粒度" not in context.extra_rules


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"family_name": "fam", "granularity": "1h", "explicit_granularity": True}, True),
        ({"family_name": "fam", "granularity": "1h", "explicit_granularity": False}, False),
        ({"family_name": "fam", "granularity": "1h"}, False),
    ],
    ids=["named", "derived", "field-omitted"],
)
async def test_business_twin_selector_reports_whether_the_question_named_the_granularity(
    payload: dict[str, Any], expected: bool
) -> None:
    """A missing or non-true value must read as derived rather than forcing `time`."""
    selector = BusinessTwinTableSelector(
        make_context("udn.business_twin", scripted_model(_fenced_json(payload)), None),
        base=BT_BASE,
        semantic_client=None,
    )

    selection = await selector._select_table_family("question", BT_FAMILIES)

    assert selection is not None
    assert selection["explicit_granularity"] is expected


# --- traffic_insight injection -------------------------------------------------


async def _traffic_insight_context(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    *,
    explicit_granularity: bool,
) -> SchemaContext:
    """Run traffic_insight ``select()`` with schema linking stubbed to one granularity verdict."""
    selector = TrafficInsightTableSelector(
        make_context("udn.traffic_insight", scripted_model(), None, db_id="yunhe"),
        base=TI_BASE,
        semantic_client=None,
    )

    async def fake_schema_linking(_question: str):
        return {}, [], explicit_granularity

    monkeypatch.setattr(selector, "_schema_linking", fake_schema_linking)
    return await selector.select(question)


async def test_traffic_insight_named_granularity_makes_time_mandatory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selector already tells a named granularity apart; nodes need not re-read it."""
    context = await _traffic_insight_context(
        monkeypatch, TI_NAMED_QUESTION, explicit_granularity=True
    )

    rules = context.extra_rules
    assert "\n\n## 时间粒度" in rules
    assert "`time` 必须出现在 SELECT 和 GROUP BY" in rules
    assert "不得把结果压成单行" in rules
    assert "任何要求移除 `time` 的意见都应判为无效" in rules
    # traffic_insight gets the bare date rule: no relative-time parsing hint, by design.
    assert "现在为" in rules
    assert "请基于当前日期解析用户问题中的相对时间" not in rules


async def test_traffic_insight_derived_granularity_leaves_time_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A derived granularity only picks the table; it must not force a per-bucket output."""
    context = await _traffic_insight_context(
        monkeypatch, TI_DERIVED_QUESTION, explicit_granularity=False
    )

    assert "## 时间粒度" not in context.extra_rules


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"family_name": "fam", "granularity": "1h", "explicit_granularity": True}, True),
        ({"family_name": "fam", "granularity": "1h", "explicit_granularity": False}, False),
        ({"family_name": "fam", "granularity": "1h"}, False),
    ],
    ids=["named", "derived", "field-omitted"],
)
async def test_traffic_insight_selector_reports_whether_the_question_named_the_granularity(
    payload: dict[str, Any], expected: bool
) -> None:
    """A missing or non-true value must read as derived rather than forcing `time`."""
    selector = TrafficInsightTableSelector(
        make_context(
            "udn.traffic_insight", scripted_model(_fenced_json(payload)), None, db_id="yunhe"
        ),
        base=TI_BASE,
        semantic_client=None,
    )

    selection = await selector._select_traffic_insight_table_family("question", TI_FAMILIES)

    assert selection is not None
    assert selection["explicit_granularity"] is expected
