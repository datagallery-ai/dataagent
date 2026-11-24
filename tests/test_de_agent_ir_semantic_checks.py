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
"""Unit tests for DataTaskIR deterministic semantic checks and template guidance."""

from __future__ import annotations

import unittest
from pathlib import Path

from dataagent.extensions.loading import PythonLoader

_LOADER = PythonLoader()
_PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


def _module(name):
    import sys

    _LOADER.load(_PLUGIN, "hooks/ir.py:core")
    namespace = _LOADER._namespace(_PLUGIN)
    return sys.modules[f"{namespace}.utils.data_task_ir.{name}"]


fill = _module("fill")
_render_predicate = _module("render")._render_predicate
format_ir_caliber_review_pending = _module("render").format_ir_caliber_review_pending
render_caliber_review_structured_summary = _module(
    "render"
).render_caliber_review_structured_summary
render_review_summary = _module("render").render_review_summary
collect_ir_semantic_warnings = _module("semantic_checks").collect_ir_semantic_warnings
list_field_templates = _module("template").list_field_templates
load_template_catalog = _module("template").load_template_catalog


def _fields(**values):
    return [{"field_id": key, "value": value, "unresolved": []} for key, value in values.items()]


class IrSemanticCheckTests(unittest.TestCase):
    def _join_search_fields(self, *, identity_refs: list[str]):
        return _fields(
            identity={
                "components": [
                    {
                        "field_ref": ref,
                        "semantic_role": "键",
                        "null_semantics": "reject_record",
                    }
                    for ref in identity_refs
                ],
                "uniqueness_scope": "final_dataset",
            },
            sources={
                "facts": [{"source_ref": "recall", "filters": [], "derived": [], "dedup": None}],
                "dimensions": [
                    {
                        "source_ref": "app_dim",
                        "filters": [],
                        "derived": [],
                        "dedup": None,
                        "join": {
                            "fact_source_ref": "recall",
                            "join_type": "inner",
                            "conditions": [{"fact_field": "query", "dim_field": "app_cn_name"}],
                            "cardinality": "many_to_one",
                            "unmatched_fact": "drop",
                        },
                    }
                ],
            },
        )

    def test_join_key_as_identity_warns_for_app_subject(self):
        fields = self._join_search_fields(identity_refs=["query"])
        warnings = collect_ir_semantic_warnings(
            fields,
            user_query="为各应用开发使用中文名作为搜索词时AI Search召回的应用",
        )
        self.assertTrue(any("JOIN 关联键" in w for w in warnings), warnings)
        self.assertTrue(any("app_id" in w for w in warnings), warnings)
        self.assertFalse(any("device_id" in w for w in warnings), warnings)

    def test_join_key_as_identity_warns_device_subject_not_app_id(self):
        fields = self._join_search_fields(identity_refs=["query"])
        warnings = collect_ir_semantic_warnings(
            fields,
            user_query="按设备统计用中文名作为搜索词召回的结果",
        )
        self.assertTrue(any("JOIN 关联键" in w for w in warnings), warnings)
        self.assertTrue(any("device_id" in w for w in warnings), warnings)
        self.assertFalse(any("通常 app_id" in w for w in warnings), warnings)

    def test_join_key_ok_when_device_entity_key_present(self):
        fields = self._join_search_fields(identity_refs=["device_id", "query"])
        warnings = collect_ir_semantic_warnings(
            fields,
            user_query="按设备统计用中文名作为搜索词召回的结果",
        )
        self.assertFalse(any("JOIN 关联键" in w for w in warnings), warnings)

    def test_join_key_ok_when_app_entity_key_present(self):
        fields = self._join_search_fields(identity_refs=["app_id", "query"])
        warnings = collect_ir_semantic_warnings(
            fields,
            user_query="为各应用开发使用中文名作为搜索词时AI Search召回的应用",
        )
        self.assertFalse(any("JOIN 关联键" in w for w in warnings), warnings)

    def test_list_agg_without_evidence_warns(self):
        fields = _fields(
            output_features={
                "layout": "flat",
                "axes": [],
                "measures": [],
                "overrides": [],
                "extras": [],
                "features": [
                    {
                        "field_name": "recalled_apps",
                        "role": "metric",
                        "output_type": "string",
                        "description": "召回",
                        "source": "recall.agent_recall_apps",
                        "calc": {"intent": "collect_list 拼接召回", "on": "agent_recall_apps"},
                        "aggregation": {
                            "kind": "list_agg",
                            "grain": ["app_id"],
                            "count_type": None,
                            "count_entity": None,
                            "distinct_field": None,
                            "expression_template": None,
                        },
                        "filters": [],
                        "window_ref": None,
                        "ratio": None,
                        "rank": None,
                        "depends_on": [],
                    }
                ],
            }
        )
        warnings = collect_ir_semantic_warnings(fields, user_query="为各应用输出召回结果")
        self.assertTrue(any("list_agg" in w for w in warnings), warnings)

    def test_passthrough_ok_without_list_agg_warning(self):
        fields = _fields(
            output_features={
                "layout": "flat",
                "axes": [],
                "measures": [],
                "overrides": [],
                "extras": [],
                "features": [
                    {
                        "field_name": "recalled_apps",
                        "role": "metric",
                        "output_type": "string",
                        "description": "召回",
                        "source": "recall.agent_recall_apps",
                        "calc": {
                            "intent": "透传已有召回串，不做多行拼接",
                            "on": "agent_recall_apps",
                        },
                        "aggregation": {
                            "kind": "passthrough",
                            "grain": ["app_id"],
                            "count_type": None,
                            "count_entity": None,
                            "distinct_field": None,
                            "expression_template": None,
                        },
                        "filters": [],
                        "window_ref": None,
                        "ratio": None,
                        "rank": None,
                        "depends_on": [],
                    }
                ],
            }
        )
        warnings = collect_ir_semantic_warnings(fields, user_query="为各应用输出召回结果")
        self.assertFalse(any("list_agg" in w or "collect_list" in w for w in warnings), warnings)


class IrTemplateGuidanceTests(unittest.TestCase):
    def test_catalog_exposes_kind_and_join_identity_guidance(self):
        catalog = load_template_catalog()
        self.assertEqual(catalog.get("template_version"), "1.2.0")
        contract = "\n".join(catalog.get("consistency_contract") or [])
        self.assertIn("JOIN 关联键", contract)
        self.assertIn("passthrough", contract)
        self.assertIn("pt_h", contract)
        self.assertIn("当天全部小时", contract)
        self.assertNotIn("仅海外", contract)
        templates = {t["field_id"]: t for t in list_field_templates(catalog)}
        identity_prompt = "\n".join(
            templates["identity"].get("field_instructions", {}).get("global_rules", [])
        )
        self.assertIn("关联键", identity_prompt)
        time_rules = "\n".join(
            templates["time_context"].get("field_instructions", {}).get("global_rules", [])
        )
        self.assertIn("小时分区", time_rules)
        grain_enum = templates["time_context"]["value_schema"]["properties"]["output_bucket"][
            "properties"
        ]["grain"]["enum"]
        self.assertIn("hour", grain_enum)
        of_rules = "\n".join(
            templates["output_features"].get("field_instructions", {}).get("global_rules", [])
        )
        self.assertIn("aggregation.kind", of_rules)
        self.assertIn("passthrough", of_rules)
        measures_agg = templates["output_features"]["value_schema"]["properties"]["measures"][
            "items"
        ]["properties"]["aggregation"]["properties"]
        self.assertIn("kind", measures_agg)
        sources = templates["sources"]
        self.assertEqual(fill.field_structural_errors(sources, {"value": None}), [])
        sources_rules = "\n".join(sources.get("field_instructions", {}).get("global_rules", []))
        self.assertIn("pt_h", sources_rules)
        mapping = "\n".join(
            catalog.get("predicate_protocol", {}).get("mapping_rules_inherited", [])
        )
        self.assertNotIn("仅海外", mapping)

    def test_hour_table_without_pt_h_warns(self):
        fields = _fields(
            sources={
                "facts": [
                    {
                        "source_ref": "biads.ads_agalg_calibration_bayes_smoother_pcvr_siteid_appid_apptype_hm",
                        "filters": [
                            {"field_ref": "pt_d", "operator": "eq", "operands": ["$date"]},
                        ],
                        "derived": [],
                        "dedup": None,
                    }
                ],
                "dimensions": [],
            },
            time_context={
                "output_bucket": {
                    "time_field": "pt_d",
                    "grain": "day",
                    "timezone": None,
                    "calendar": None,
                    "week_start": None,
                    "custom_definition": None,
                },
                "input_windows": [],
            },
        )
        warnings = collect_ir_semantic_warnings(fields, user_query="按榜单聚合校准系数")
        self.assertTrue(any("pt_h" in w or "小时" in w for w in warnings), warnings)

    def test_hour_table_with_pt_h_and_grain_ok(self):
        fields = _fields(
            sources={
                "facts": [
                    {
                        "source_ref": "biads.ads_foo_hm",
                        "filters": [
                            {"field_ref": "pt_d", "operator": "eq", "operands": ["$date"]},
                            {"field_ref": "pt_h", "operator": "eq", "operands": ["$hour"]},
                        ],
                        "derived": [],
                        "dedup": None,
                    }
                ],
                "dimensions": [],
            },
            time_context={
                "output_bucket": {
                    "time_field": "pt_h",
                    "grain": "hour",
                    "timezone": None,
                    "calendar": None,
                    "week_start": None,
                    "custom_definition": None,
                },
                "input_windows": [],
            },
        )
        warnings = collect_ir_semantic_warnings(fields, user_query="按榜单聚合校准系数")
        self.assertFalse(any("小时口径不完整" in w for w in warnings), warnings)

    def test_hour_table_all_day_confirmed_ok(self):
        fields = _fields(
            sources={
                "facts": [
                    {
                        "source_ref": "biads.ads_foo_hm",
                        "filters": [
                            {"field_ref": "pt_d", "operator": "eq", "operands": ["$date"]},
                        ],
                        "derived": [],
                        "dedup": None,
                    }
                ],
                "dimensions": [],
            },
            time_context={
                "output_bucket": {
                    "time_field": "pt_d",
                    "grain": "day",
                    "timezone": None,
                    "calendar": None,
                    "week_start": None,
                    "custom_definition": "当天全部小时聚合（源为小时表，不按 pt_h 切片；写入仅天分区）",
                },
                "input_windows": [],
            },
        )
        # Attach rationale on time_context field for blob matching
        for f in fields:
            if f["field_id"] == "time_context":
                f["rationale"] = "已确认当天全部小时聚合"
        warnings = collect_ir_semantic_warnings(fields, user_query="按日汇总校准系数")
        self.assertFalse(any("小时口径不完整" in w for w in warnings), warnings)
        self.assertFalse(any("仍过滤 pt_h" in w for w in warnings), warnings)

    def test_hour_table_all_day_claim_with_pt_h_warns(self):
        fields = _fields(
            sources={
                "facts": [
                    {
                        "source_ref": "biads.ads_foo_hm",
                        "filters": [
                            {"field_ref": "pt_d", "operator": "eq", "operands": ["$date"]},
                            {"field_ref": "pt_h", "operator": "eq", "operands": ["$hour"]},
                        ],
                        "derived": [],
                        "dedup": None,
                    }
                ],
                "dimensions": [],
            },
            time_context={
                "output_bucket": {
                    "time_field": "pt_d",
                    "grain": "day",
                    "timezone": None,
                    "calendar": None,
                    "week_start": None,
                    "custom_definition": "当天全部小时聚合",
                },
                "input_windows": [],
            },
        )
        warnings = collect_ir_semantic_warnings(fields, user_query="按日汇总")
        self.assertTrue(any("仍过滤 pt_h" in w or "全天小时" in w for w in warnings), warnings)

    def test_hour_table_all_day_and_single_hour_conflict_warns(self):
        """全天声明 + grain=hour/pt_d-only 不得静默放行。"""
        fields = _fields(
            sources={
                "facts": [
                    {
                        "source_ref": "biads.ads_foo_hm",
                        "filters": [
                            {"field_ref": "pt_d", "operator": "eq", "operands": ["$date"]},
                        ],
                        "derived": [],
                        "dedup": None,
                    }
                ],
                "dimensions": [],
            },
            time_context={
                "output_bucket": {
                    "time_field": "pt_h",
                    "grain": "hour",
                    "timezone": None,
                    "calendar": None,
                    "week_start": None,
                    "custom_definition": "当天全部小时聚合",
                },
                "input_windows": [],
            },
        )
        warnings = collect_ir_semantic_warnings(fields, user_query="按日汇总")
        self.assertTrue(any("自相矛盾" in w for w in warnings), warnings)

    def test_hour_table_all_day_without_pt_d_warns(self):
        """已确认全天但 filters 空（无 pt_d）必须告警。"""
        fields = _fields(
            sources={
                "facts": [
                    {
                        "source_ref": "biads.ads_foo_hm",
                        "filters": [],
                        "derived": [],
                        "dedup": None,
                    }
                ],
                "dimensions": [],
            },
            time_context={
                "output_bucket": {
                    "time_field": "pt_d",
                    "grain": "day",
                    "timezone": None,
                    "calendar": None,
                    "week_start": None,
                    "custom_definition": "当天全部小时聚合",
                },
                "input_windows": [],
            },
        )
        warnings = collect_ir_semantic_warnings(fields, user_query="按日汇总")
        self.assertTrue(any("未按 pt_d 过滤" in w for w in warnings), warnings)


class IrRenderSemanticTests(unittest.TestCase):
    def test_kind_render_business_language(self):
        text = _render_predicate(
            {"field_ref": "frame_type", "operator": "eq", "operands": ["0"]},
            "app_dim",
        )
        self.assertIn("等于", text)
        fields = [
            {
                "field_id": "output_features",
                "value": {
                    "layout": "flat",
                    "axes": [],
                    "measures": [],
                    "overrides": [],
                    "extras": [],
                    "features": [
                        {
                            "field_name": "recalled_apps",
                            "role": "metric",
                            "output_type": "string",
                            "description": "召回",
                            "source": "recall.agent_recall_apps",
                            "calc": {"intent": "透传已有召回串", "on": "agent_recall_apps"},
                            "aggregation": {
                                "kind": "passthrough",
                                "grain": ["app_id"],
                            },
                            "filters": [],
                        }
                    ],
                },
                "unresolved": [],
            }
        ]
        summary = render_review_summary(fields)
        self.assertIn("透传", summary)

    def test_caliber_review_pending_is_checklist_first(self):
        fields = [
            {
                "field_id": "identity",
                "value": {
                    "components": [
                        {
                            "field_ref": "user_id",
                            "semantic_role": "账号",
                            "null_semantics": "reject_record",
                        }
                    ],
                    "uniqueness_scope": "final_dataset",
                },
                "unresolved": [],
            },
            {
                "field_id": "output_features",
                "value": {
                    "layout": "cartesian",
                    "axes": [
                        {
                            "axis_id": "window",
                            "values": ["w1", "w2", "w3", "w4", "w5", "w6"],
                        }
                    ],
                    "measures": [
                        {
                            "measure_id": f"m{i}",
                            "name_pattern": f"col_{{window}}_{i}",
                            "calc": {"intent": "序列", "on": "app_id"},
                            "aggregation": {"grain": ["user_id"]},
                        }
                        for i in range(6)
                    ],
                    "overrides": [],
                    "extras": [],
                    "features": [],
                },
                "unresolved": [],
            },
        ]
        structured, risks = render_caliber_review_structured_summary(fields)
        self.assertIn("主键/粒度", structured)
        self.assertIn("输出形态", structured)
        self.assertTrue(risks, risks)
        text = format_ir_caliber_review_pending(fields)
        self.assertLess(text.find("请留意"), text.find("回答方式"))
        self.assertIn("请优先留意", text)
        self.assertIn("结构化摘要", text)

    def test_caliber_summary_all_day_hours_confirmed(self):
        fields = _fields(
            sources={
                "facts": [
                    {
                        "source_ref": "biads.ads_foo_hm",
                        "filters": [
                            {"field_ref": "pt_d", "operator": "eq", "operands": ["$date"]},
                        ],
                        "derived": [],
                        "dedup": None,
                    }
                ],
                "dimensions": [],
            },
            time_context={
                "output_bucket": {
                    "time_field": "pt_d",
                    "grain": "day",
                    "timezone": None,
                    "calendar": None,
                    "week_start": None,
                    "custom_definition": "当天全部小时聚合（不按 pt_h 切片）",
                },
                "input_windows": [],
            },
        )
        structured, risks = render_caliber_review_structured_summary(fields)
        self.assertIn("当天全部小时", structured)
        self.assertFalse(
            any("未见小时过滤" in r or "未见 pt_h" in r for r in risks),
            risks,
        )


if __name__ == "__main__":
    unittest.main()
