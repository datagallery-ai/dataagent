"""Six-field catalog, deterministic expansion and consumer contracts from !479."""

from copy import deepcopy
from pathlib import Path

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


def functions():
    loader = PythonLoader()
    expand = loader.load(PLUGIN, "utils/data_task_ir/render.py:expand_output_features")
    render = loader.load(PLUGIN, "utils/data_task_ir/render.py:render_constraint_context")
    catalog = loader.load(PLUGIN, "utils/data_task_ir/template.py:load_template_catalog")
    return expand, render, catalog


def cartesian():
    return {
        "layout": "cartesian",
        "axes": [{"axis_id": "window", "values": ["7d", "14d", "30d"]}],
        "measures": [
            {
                "measure_id": name,
                "name_pattern": "{measure_id}_{window}",
                "aggregation": {"grain": ["user_id"]},
            }
            for name in ("count", "amount", "ratio")
        ],
    }


def test_catalog_is_v1_and_all_six_units_can_render_their_examples():
    _, render, load = functions()
    catalog = load()
    assert str(catalog["template_version"]) == "1.0.0"
    assert [field["field_id"] for field in catalog["fields"]] == [
        "identity",
        "sources",
        "time_context",
        "output_features",
        "pipeline",
        "sequence",
    ]
    # Exercise actual template structures rather than mocked field definitions.
    for field in catalog["fields"]:
        for example in field.get("few_shots", []):
            output = example.get("output")
            if isinstance(output, dict):
                assert render([output])


def test_cartesian_expansion_keeps_every_combination_and_input_unchanged():
    expand, _, _ = functions()
    value = cartesian()
    original = deepcopy(value)
    columns = expand(value)
    assert [column["field_name"] for column in columns] == [
        f"{metric}_{window}"
        for window in ("7d", "14d", "30d")
        for metric in ("count", "amount", "ratio")
    ]
    assert value == original
    columns[0]["aggregation"]["grain"].append("changed")
    assert value == original


def test_partial_window_catalog_example_expands_to_eight_business_columns():
    expand, _, load = functions()
    features = next(field for field in load()["fields"] if field["field_id"] == "output_features")
    example = next(item for item in features["few_shots"] if "3×2+2=8" in item["input"])
    value = example["output"]["value"]
    columns = expand(value)
    assert len(value["measures"]) == 2
    assert len(value["extras"]) == 2
    assert len(columns) == 8
    assert [column["field_name"] for column in columns[-2:]] == ["feat_c", "feat_d"]
    assert all(column["window_ref"] == "w21d" for column in columns[-2:])


def test_overrides_apply_only_to_selected_combination_and_rank_tracks_base():
    expand, _, _ = functions()
    value = cartesian()
    value["measures"] = value["measures"][:1]
    value["measures"][0]["attach_rank"] = {
        "rank_type": "ntile",
        "bucket_count": 100,
        "order_direction": "desc",
        "partition_by": [],
    }
    value["overrides"] = [
        {
            "measure_id": "count",
            "binding": {"window": "30d"},
            "patch": {"aggregation": {"grain": ["user_id", "app_id"]}},
        }
    ]
    value["extras"] = [{"field_name": "label", "calc": {"intent": "分类"}}]
    columns = expand(value)
    assert len(columns) == 7
    for index in (0, 2, 4):
        base, rank = columns[index : index + 2]
        assert rank["depends_on"] == [base["field_name"]]
        assert rank["rank"]["order_by"] == base["field_name"]
        assert rank["rank"]["bucket_count"] == 100
    assert columns[0]["aggregation"]["grain"] == ["user_id"]
    assert columns[4]["aggregation"]["grain"] == ["user_id", "app_id"]
    assert columns[-1]["field_name"] == "label"


def test_flat_and_unknown_axes_do_not_invent_columns():
    expand, _, _ = functions()
    value = {"layout": "flat", "features": [{"field_name": "total"}, {}]}
    columns = expand(value)
    assert columns == [{"field_name": "total"}]
    columns[0]["field_name"] = "changed"
    assert value["features"][0]["field_name"] == "total"
    value = cartesian()
    value["axes"][0]["values"] = []
    assert expand(value) == []
    assert expand(None) == []


def test_render_counts_key_plus_expansion_and_preserves_per_feature_grain():
    _, render, _ = functions()
    value = cartesian()
    value["measures"][1]["aggregation"]["grain"] = ["user_id", "app_id"]
    fields = [
        {"field_id": "identity", "value": {"components": [{"field_ref": "user_id"}]}},
        {"field_id": "output_features", "value": value},
    ]
    text = render(fields)
    assert "当前已记录的输出语义项共 10" in text
    assert "SQL/DDL 不强制使用同名" in text
    assert "count_7d=[user_id]" in text
    assert "amount_7d=[user_id, app_id]" in text
    assert "禁止用单一全局 GROUP BY" in text
    assert "diagnostic-only" not in text
    assert text.count("【约束遵循规则 - 必须遵守】") == 1
    assert render([]) == ""


def test_checker_and_feature_engineering_skill_use_six_field_semantics():
    checker = (PLUGIN / "prompts/ir_quality_check.md").read_text()
    skill = (PLUGIN / "skills/feature-engineering/SKILL.md").read_text()
    for prompt in (checker, skill):
        assert "identity.components" in prompt
        assert "expand(output_features)" in prompt
        assert "aggregation.grain" in prompt
        assert "feature_key" not in prompt
        assert "output_fields" not in prompt
    assert "不得因列名不一致判失败" in checker
