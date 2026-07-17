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
import json
import re
from typing import Any

from dataagent.agents.nl2sql.errors import NL2SQLError
from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import json_parser, schema_to_ddl
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.utils.log import logger

_UDN_DIMENSION_METADATA = {
    1: {
        "field": "mos4_qds",
        "name": "保障&MOS四象限",
        "description": "按保障状态与MOS质量优劣划分用户四象限",
    },
    2: {
        "field": "guarantee_group",
        "name": "保障分群",
        "description": "区分保障签约用户质差前、质差未保障、质差保障中和未签约用户",
    },
    3: {
        "field": "crh_group",
        "name": "高铁分群",
        "description": "按高铁场景和用户属性划分用户群",
    },
    4: {
        "field": "default5qi_group",
        "name": "默载5QI分群",
        "description": "按默认承载5QI值划分用户，例如5QI=6",
    },
    5: {
        "field": "term_brand",
        "name": "终端品牌分群",
        "description": "按苹果、华为、小米、荣耀、OPPO、VIVO等终端品牌划分用户",
    },
    6: {
        "field": "custom_group",
        "name": "自定义分群",
        "description": "按业务配置的自定义用户群划分用户",
    },
    7: {
        "field": "app_id",
        "name": "业务分类",
        "description": "业务大类，例如游戏、直播、即时通信、视频和办公",
    },
    8: {
        "field": "sub_app_id",
        "name": "应用",
        "description": "具体应用，例如抖音、快手、王者荣耀",
    },
    9: {
        "field": "supi",
        "name": "SUPI",
        "description": "用户永久标识，通常对应SUPI或IMSI",
    },
    10: {
        "field": "gpsi",
        "name": "GPSI",
        "description": "用户公共标识，通常对应包含国家码的手机号",
    },
    11: {
        "field": "tai",
        "name": "跟踪区",
        "description": "移动网络跟踪区标识TAI",
    },
    12: {
        "field": "gnb",
        "name": "基站标识",
        "description": "5G基站gNodeB标识",
    },
    13: {
        "field": "cell_id",
        "name": "小区标识",
        "description": "5G小区标识，用于各小区或指定小区分析",
    },
    14: {
        "field": "province",
        "name": "省份",
        "description": "省级行政区划",
    },
    15: {
        "field": "city",
        "name": "地市",
        "description": "地级市或直辖市行政区划，例如无锡市",
    },
    16: {
        "field": "county",
        "name": "区县",
        "description": "区县级行政区划，例如锡山区",
    },
    17: {
        "field": "cell_ul_group",
        "name": "小区上行PRB分群",
        "description": "按小区上行PRB负载程度划分小区",
    },
    18: {
        "field": "ne_name",
        "name": "网元名称",
        "description": "AMF、PCF或NWDAF等网元实例名称",
    },
    19: {
        "field": "cell_dl_group",
        "name": "小区下行PRB分群",
        "description": "按小区下行PRB负载程度划分小区",
    },
    20: {
        "field": "info_indicate",
        "name": "体验单据类型",
        "description": "区分保障用户EXP单据、体验用户EXP单据等体验信息单据类型",
    },
    21: {
        "field": "user_type",
        "name": "高铁用户类型",
        "description": "区分CRH高铁用户等高铁用户画像类型",
    },
    22: {
        "field": "crh_sub_ind",
        "name": "高铁签约标识",
        "description": "标识用户是否为高铁签约用户",
    },
}


class UDNPerceptorNode(PerceptorNode):
    _UDN_TABLE_RE = re.compile(
        r"^(?:[^.]+\.)?fact_(?P<business_id>dw\d+)_"
        r"(?P<dimension_code>[0-9a-fA-F]{16})_metric_(?P<granularity>5min|15min|1h|1d)$"
    )
    _UDN_GRANULARITY_ORDER = {"5min": 0, "15min": 1, "1h": 2, "1d": 3}
    _UDN_SCHEMA_PREFIX = "udn."

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        udn_cfg: dict = self._get_agent_config("SEMANTIC_LAYER.udn", {}) or {}
        table_cfg: dict = udn_cfg.get("table_selection", {})
        table_selection_mode = table_cfg.get("mode", "business_family")
        if table_selection_mode != "business_family":
            raise ValueError("SEMANTIC_LAYER.udn.table_selection.mode must be 'business_family'")
        self.table_llm_topk = table_cfg.get("llm_topk", 4)

    @staticmethod
    def _decode_udn_dimensions(dimension_code: str) -> list[str]:
        value = int(str(dimension_code), 16)
        return [metadata["field"] for bit, metadata in _UDN_DIMENSION_METADATA.items() if value & (1 << bit)]

    @staticmethod
    def _format_udn_table_family_prompt_context(families: list[dict[str, Any]]) -> str:
        metadata_by_field = {metadata["field"]: metadata for metadata in _UDN_DIMENSION_METADATA.values()}
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
            granularities = ", ".join(f"`{value}`" for value in family["available_granularities"])
            lines.extend(
                [
                    f"### {index}. `{family['family_name']}`",
                    f"- 表簇包含维度：{dimensions}",
                    f"- 表簇可用时间粒度：{granularities}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    @classmethod
    def _parse_udn_table_name(cls, table_name: str) -> dict[str, str] | None:
        name = str(table_name or "").strip()
        match = cls._UDN_TABLE_RE.match(name)
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
    def _build_udn_table_family_candidates(
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
                key=lambda item: cls._UDN_GRANULARITY_ORDER.get(item[0], 999),
            )
            families.append(
                {
                    "family_name": f"fact_{business_id}_{dimension_code}",
                    "dimensions": cls._decode_udn_dimensions(dimension_code),
                    "available_granularities": [granularity for granularity, _ in pairs],
                    "candidate_table_names": [table_name for _, table_name in pairs],
                }
            )
        return families

    @classmethod
    def _resolve_udn_table_family_selection(
        cls, selection: dict[str, str] | None, families: list[dict[str, Any]]
    ) -> str | None:
        if not selection:
            return None
        family_name = selection["family_name"].removeprefix(cls._UDN_SCHEMA_PREFIX)
        for family in families:
            if family["family_name"] != family_name:
                continue
            for granularity, table_name in zip(
                family["available_granularities"], family["candidate_table_names"], strict=True
            ):
                if granularity == selection["granularity"]:
                    return table_name
        return None

    def udn_schema_linking(self, question: str):
        tables = self._select_udn_tables_by_business_family(question)
        schema, joins = self.full_schema(allow_tables=tables)
        for table in schema.values():
            for column in table["columns"].values():
                column["example_values"] = "|".join(
                    item.replace(":", "=", 1).removesuffix("=")
                    for item in column.get("example_values", "").split("|")
                    if item
                )
        return schema, joins, self._udn_column_metadata()

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        state["sql_rules"] = self._load_prompt(self.user_sql_rules)
        schema, joins, catalog = self.udn_schema_linking(state["question"])
        state["schema"] = schema
        state["joins"] = joins
        state["schema_str"] = schema_to_ddl(schema, joins, catalog)
        message = f"=== Perceptor ===\n{state['schema_str']}"
        logger.info(message)
        state["stream_message"] = message
        return state

    def _udn_column_metadata(self) -> dict[str, dict[str, Any]]:
        return {
            str(col_key): dict(meta)
            for col_key, meta in self._get_table_columns_info("udn.derived_metrics").items()
            if isinstance(meta, dict)
        }

    def _udn_full_table_catalog(self) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for item in self._get_table_list():
            if not isinstance(item, dict) or not item:
                continue
            table_name = next(iter(item))
            parsed = self._parse_udn_table_name(table_name)
            if parsed:
                catalog.append(parsed)
        return catalog

    def _select_udn_business_ids(self, question: str) -> list[str]:
        response = self.execute_with_llm(
            {"question": question, "top_n": self.table_llm_topk}, action="filter_udn_business_id_"
        )
        values = json.loads(json_parser(response))
        business_ids: list[str] = []
        for value in values:
            match = re.search(r"dw\d+", str(value))
            if match and match.group(0) not in business_ids:
                business_ids.append(match.group(0))
        return business_ids[: self.table_llm_topk]

    def _select_udn_table_family(self, question: str, families: list[dict[str, Any]]) -> dict[str, str] | None:
        response = self.execute_with_llm(
            {"question": question, "tables": self._format_udn_table_family_prompt_context(families)},
            action="filter_udn_table_family_",
        )
        parsed = json.loads(json_parser(response))
        family_name = str(parsed.get("family_name") or "").strip()
        granularity = str(parsed.get("granularity") or "").strip()
        return {"family_name": family_name, "granularity": granularity} if family_name and granularity else None

    def _select_udn_tables_by_business_family(self, question: str) -> list[str]:
        business_ids = self._select_udn_business_ids(question)
        if not business_ids:
            raise NL2SQLError("UDN business ID selection returned no valid result")
        families = self._build_udn_table_family_candidates(self._udn_full_table_catalog(), business_ids)
        if not families:
            raise NL2SQLError(
                "UDN table family catalog is empty",
                detail=f"No table family matched business IDs: {', '.join(business_ids)}",
            )
        selection = self._select_udn_table_family(question, families)
        table = self._resolve_udn_table_family_selection(selection, families)
        if not table:
            raise NL2SQLError("UDN table family selection returned no valid table")
        return [table]
