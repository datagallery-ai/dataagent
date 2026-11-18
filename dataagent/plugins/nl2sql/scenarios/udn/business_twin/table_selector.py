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
"""business_twin table selection: pinned tables or the business-family pipeline.

Ported from the legacy ``business_twin_perceptor``. Schema fetching reuses the
base :class:`PerceptorNode` (an instance bound to the same config and to the
same scenario-carrying semantic client), so table/column/join retrieval and its
error paths stay identical to the default flow. The pack's heavy assets
(business-ID catalog, administrative divisions, prompts, SQL rules) load once
at assembly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nl2sql.errors import DataAgentError
from nl2sql.hooks import HookContext, SchemaContext
from nl2sql.nodes.perceptor import PerceptorNode
from nl2sql.utils.nl2sql_utils import schema_to_ddl

from ..common.llm import execute_with_llm_json
from ..common.rules import current_date_rule, time_granularity_rules
from .data.administrative_divisions import format_administrative_division_rules
from .data.business_twin_business_id_selector import (
    BusinessTwinExtraction,
    select_business_twin_business_id,
)
from .data.business_twin_dimension_metadata import BUSINESS_TWIN_DIMENSION_METADATA

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from dataagent import SemanticServiceClient


def _scenario_error(message: str, detail: str = "") -> DataAgentError:
    """Map a legacy NL2SQLError(message, detail=...) onto the plugin's error type."""
    fact = f"{message}: {detail}" if detail else message
    return DataAgentError(source="internal", component="nl2sql", fact=fact)


class BusinessTwinTableSelector:
    """Selects the business-twin fact table and builds the run's SchemaContext."""

    _TABLE_RE = re.compile(
        r"^(?:[^.]+\.)?fact_(?P<business_id>dw\d+)_"
        r"(?P<dimension_code>[0-9a-fA-F]{16})_metric_(?P<granularity>5min|15min|1h|1d)$"
    )
    # 从自然语言中提取表名提及（忽略大小写），表名前后紧邻字母/数字/下划线时不匹配；
    # 表名格式为：fact_dw<数字>_<16位十六进制>_metric_(5min|15min|1h|1d)
    _TABLE_MENTION_RE = re.compile(
        r"(?<![A-Za-z0-9_])"
        r"(?P<table_name>fact_dw[0-9]+_[0-9a-fA-F]{16}_metric_(?:5min|15min|1h|1d))"
        r"(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )
    _GRANULARITY_ORDER = {"5min": 0, "15min": 1, "1h": 2, "1d": 3}
    _EXPLICIT_ONLY_TABLES = frozenset({"fact_dw1745159004_0000000000000000_metric_1h"})

    def __init__(
        self,
        ctx: HookContext,
        *,
        base: Path,
        semantic_client: SemanticServiceClient | None,
    ):
        self._model = ctx.model
        self._prompt_dir = base / "prompts"
        self._sql_rules = (base / "sql_rules.md").read_text(encoding="utf-8")
        self._perceptor = PerceptorNode(config=ctx.config)
        self._perceptor.bind_semantic_client(semantic_client)

    @staticmethod
    def _decode_dimensions(dimension_code: str) -> list[str]:
        value = int(str(dimension_code), 16)
        return [
            metadata["field"]
            for bit, metadata in BUSINESS_TWIN_DIMENSION_METADATA.items()
            if value & (1 << bit)
        ]

    @staticmethod
    def _format_table_family_prompt_context(families: list[dict[str, Any]]) -> str:
        metadata_by_field = {
            metadata["field"]: metadata for metadata in BUSINESS_TWIN_DIMENSION_METADATA.values()
        }
        dimension_fields: list[str] = []
        for family in families:
            for field in family["dimensions"]:
                if field in metadata_by_field and field not in dimension_fields:
                    dimension_fields.append(field)

        lines = ["## 维度说明"]
        for field in dimension_fields:
            metadata = metadata_by_field[field]
            lines.append(f"- `{field}`（{metadata['name']}）：{metadata['description']}")
        lines.extend(["", "## 候选表簇"])
        for index, family in enumerate(families, start=1):
            dimensions = ", ".join(f"`{field}`" for field in family["dimensions"])
            granularities = ", ".join(f"`{value}`" for value in family["tables_by_granularity"])
            lines.extend(
                [
                    f"### {index}. `{family['family_name']}`",
                    f"- 表簇包含维度：{dimensions}",
                    f"- 表簇可用时间粒度：{granularities}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    @staticmethod
    def _fallback_family(families: list[dict[str, Any]], required: set[str]) -> dict[str, Any]:
        """Pick the family covering the most required dimensions, then with the fewest extras."""

        def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
            index, family = item
            dimensions = set(family["dimensions"])
            return (len(required - dimensions), len(dimensions - required), index)

        eligible = [family for family in families if required <= set(family["dimensions"])]
        if not eligible:
            logger.warning(
                f"No business-twin table family covers required dimensions: {sorted(required)}; "
                "falling back to best partial coverage"
            )
            eligible = families
        return min(enumerate(eligible), key=sort_key)[1]

    @classmethod
    def _fallback_granularity(
        cls,
        family: dict[str, Any],
        selection: dict[str, Any] | None,
        extraction: BusinessTwinExtraction,
    ) -> str:
        """Pick a granularity the family really has: explicit ask, model's pick, then closest."""
        available = family["tables_by_granularity"]
        target = extraction.granularity or (selection or {}).get("granularity") or ""
        if target in available:
            return target
        order = cls._GRANULARITY_ORDER.get(target)
        if order is not None:
            finer = [g for g in available if cls._GRANULARITY_ORDER.get(g, 999) < order]
            if finer:
                return max(finer, key=lambda g: cls._GRANULARITY_ORDER[g])
            coarser = [g for g in available if cls._GRANULARITY_ORDER.get(g, 999) > order]
            if coarser:
                return min(coarser, key=lambda g: cls._GRANULARITY_ORDER[g])
        # A finer table can still answer coarser questions; the reverse cannot.
        return next(iter(available))

    @classmethod
    def _parse_table_name(cls, table_name: str) -> dict[str, str] | None:
        name = str(table_name or "").strip()
        match = cls._TABLE_RE.match(name)
        if not match:
            return None
        business_id = match.group("business_id")
        dimension_code = match.group("dimension_code").lower()
        return {
            "bare_table_name": name.rsplit(".", 1)[-1],
            "business_id": business_id,
            "dimension_code": dimension_code,
            "granularity": match.group("granularity"),
        }

    @classmethod
    def _mentioned_tables(cls, question: str) -> list[str]:
        names: list[str] = []
        for match in cls._TABLE_MENTION_RE.finditer(question):
            name = match.group("table_name").lower()
            if name not in names:
                names.append(name)
        return names

    @classmethod
    def _build_table_family_candidates(
        cls, catalog: list[dict[str, Any]], business_ids: list[str]
    ) -> list[dict[str, Any]]:
        allowed_business_ids = {str(value).strip() for value in business_ids if str(value).strip()}
        grouped: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for table in catalog:
            business_id = table["business_id"]
            if business_id not in allowed_business_ids:
                continue
            key = (business_id, table["dimension_code"])
            grouped.setdefault(key, set()).add((table["granularity"], table["bare_table_name"]))

        families: list[dict[str, Any]] = []
        for (business_id, dimension_code), table_pairs in sorted(grouped.items()):
            pairs = sorted(
                table_pairs,
                key=lambda item: cls._GRANULARITY_ORDER.get(item[0], 999),
            )
            families.append(
                {
                    "family_name": f"fact_{business_id}_{dimension_code}",
                    "dimensions": cls._decode_dimensions(dimension_code),
                    "tables_by_granularity": dict(pairs),
                }
            )
        return families

    async def select(self, question: str) -> SchemaContext:
        """Run the selection pipeline for one question; errors propagate (no silent fallback)."""
        rules = self._sql_rules + current_date_rule(relative_time_hint=True)
        rules += await asyncio.to_thread(format_administrative_division_rules, question)
        schema, joins, catalog, explicit_granularity = await self._schema_linking(question)
        if explicit_granularity:
            rules += time_granularity_rules()
        return SchemaContext(
            schema=schema,
            joins=joins,
            schema_str=schema_to_ddl(schema, joins, catalog),
            extra_rules=rules,
        )

    def _pinned_table(self, question: str, catalog: list[dict[str, Any]]) -> str | None:
        mentioned = self._mentioned_tables(question)
        if not mentioned:
            return None
        if len(mentioned) > 1:
            raise _scenario_error(
                "Business-twin question mentions multiple tables",
                f"only one table can be pinned per question: {', '.join(mentioned)}",
            )
        catalog_names = {
            table["bare_table_name"].lower(): table["bare_table_name"] for table in catalog
        }
        if mentioned[0] not in catalog_names:
            raise _scenario_error(
                "Business-twin explicitly requested table is not in the catalog",
                f"table not found in catalog: {mentioned[0]}",
            )
        return catalog_names[mentioned[0]]

    def _find_family(
        self, family_name: str, families: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        target = str(family_name or "").removeprefix(f"{self._perceptor.db}.")
        for family in families:
            if family["family_name"] == target:
                return family
        return None

    def _resolve_table(
        self,
        selection: dict[str, Any] | None,
        families: list[dict[str, Any]],
        extraction: BusinessTwinExtraction,
    ) -> tuple[str, bool]:
        """Resolve the model's family selection to a table, with a deterministic fallback."""
        family_dimensions = set().union(*(family["dimensions"] for family in families))
        dropped = set(extraction.dimensions) - family_dimensions
        if dropped:
            logger.warning(
                f"Ignoring business-twin extracted dimensions no candidate family carries: {sorted(dropped)}"
            )
        required = set(extraction.dimensions) & family_dimensions
        if selection:
            family = self._find_family(selection.get("family_name", ""), families)
            if family is not None and required <= set(family["dimensions"]):
                table = family["tables_by_granularity"].get(selection.get("granularity"))
                if table:
                    return table, selection.get("explicit_granularity") is True
        family = self._fallback_family(families, required)
        granularity = self._fallback_granularity(family, selection, extraction)
        logger.warning(
            "Business-twin table family fallback: "
            f"llm_selection={selection} required_dimensions={sorted(required)} "
            f"fallback={family['family_name']} granularity={granularity}"
        )
        return family["tables_by_granularity"][granularity], extraction.granularity is not None

    async def _schema_linking(self, question: str):
        """Select the business-twin table family and return its schema, joins, catalog, and granularity."""
        table, explicit_granularity = await self._select_table_by_business_family(question)
        schema, joins = await asyncio.to_thread(self._perceptor.full_schema, [table])
        for table in schema.values():
            for column in table["columns"].values():
                column["example_values"] = "|".join(
                    item.replace(":", "=", 1).removesuffix("=")
                    for item in column.get("example_values", "").split("|")
                    if item
                )
        catalog = await asyncio.to_thread(self._column_metadata)
        return schema, joins, catalog, explicit_granularity

    def _column_metadata(self) -> dict[str, dict[str, Any]]:
        columns = self._perceptor._get_table_columns_info(f"{self._perceptor.db}.derived_metrics")
        return {
            str(col_key): dict(meta) for col_key, meta in columns.items() if isinstance(meta, dict)
        }

    def _full_table_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for item in self._perceptor._get_table_list():
            if not isinstance(item, dict) or not item:
                continue
            table_name = next(iter(item))
            parsed = self._parse_table_name(table_name)
            if parsed:
                catalog.append(parsed)
        return catalog

    async def _select_business_id(self, question: str) -> BusinessTwinExtraction:
        payload = await execute_with_llm_json(
            self._model,
            self._prompt_dir,
            "filter_business_twin_business_id_",
            {"question": question},
        )
        logger.info(f"payload={payload}")
        try:
            return select_business_twin_business_id(payload)
        except (TypeError, ValueError) as exc:
            raise _scenario_error(
                "Business-twin column extraction returned no valid result",
                str(exc),
            ) from exc

    async def _select_table_family(
        self,
        question: str,
        families: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        parsed = await execute_with_llm_json(
            self._model,
            self._prompt_dir,
            "filter_business_twin_table_family_",
            {
                "question": question,
                "tables": self._format_table_family_prompt_context(families),
            },
        )
        family_name = str(parsed.get("family_name") or "").strip()
        granularity = str(parsed.get("granularity") or "").strip()
        if not family_name or not granularity:
            return None
        return {
            "family_name": family_name,
            "granularity": granularity,
            "explicit_granularity": parsed.get("explicit_granularity") is True,
        }

    async def _select_table_by_business_family(self, question: str) -> tuple[str, bool]:
        """Return the selected table and whether the question named its time granularity."""
        catalog = await asyncio.to_thread(self._full_table_catalog)
        pinned = self._pinned_table(question, catalog)
        if pinned:
            # Pinning names a table, not an output granularity, so nothing is claimed here.
            return pinned, False
        extraction = await self._select_business_id(question)
        families = self._build_table_family_candidates(
            [
                table
                for table in catalog
                if table["bare_table_name"].lower() not in self._EXPLICIT_ONLY_TABLES
            ],
            [extraction.business_id],
        )
        if not families:
            raise _scenario_error(
                "Business-twin table family catalog is empty",
                f"No table family matched business ID: {extraction.business_id}",
            )
        selection = await self._select_table_family(question, families)
        return self._resolve_table(selection, families, extraction)
