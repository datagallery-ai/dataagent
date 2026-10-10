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
"""get_table_schema 列枚举值（enum_values）内联/卸载/开关行为测试。"""

import json
from pathlib import Path
from typing import Any

import pytest

import dataagent.actions.tools.semantic_tool.search_tables_with_schema as stws
from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.semantic_tool.search_tables_with_schema import ENUM_VALUES_MAX_LENGTH


def _make_cols_raw(spec: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 get_table_columns_info 的返回结构；spec 为 (列名, 枚举值) 列表。

    输入键 "examples" 为 semantic-service 契约键，工具输出键为 enum_values。
    """
    return {
        f"db.t.{name}": {
            "column_short_description": f"{name}描述",
            "value_type": "string",
            "column_properties": None,
            "examples": examples,
        }
        for name, examples in spec
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    cols_raw: dict[str, Any],
    tmp_path: Path,
    *,
    sandbox_raises: bool = False,
) -> None:
    """替换 SemanticServiceClient / 表描述 / sandbox，使 get_table_schema 可离线运行。"""

    class _FakeClient:
        def get_table_columns_info(self, table_name: str, *, limit: int) -> dict[str, Any]:
            return cols_raw

    class _FakeSemanticServiceClient:
        @classmethod
        def from_config(cls, config_manager: Any) -> _FakeClient:
            return _FakeClient()

    monkeypatch.setattr(stws, "SemanticServiceClient", _FakeSemanticServiceClient)
    monkeypatch.setattr(stws, "get_table_description", lambda qualified_name, client: "表描述")

    if sandbox_raises:

        def _raise() -> None:
            raise RuntimeError("no sandbox bound to current tool call context")

        monkeypatch.setattr(stws, "get_current_sandbox", _raise)
    else:

        class _FakeSandbox:
            def __init__(self) -> None:
                self.workspace_root = tmp_path

        monkeypatch.setattr(stws, "get_current_sandbox", lambda: _FakeSandbox())


def _offload_files(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob("get_table_schema_enum_values_*.txt"))


def test_enum_values_inline_when_under_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cols_raw = _make_cols_raw(
        [
            ("status", [{"value": "open", "description": "打开"}]),
            ("owner", []),
            ("remark", None),
        ]
    )
    _install(monkeypatch, cols_raw, tmp_path)

    result = stws.get_table_schema("db.t", _tool_context=ToolExecutionContext(tool_config={}))
    detail = result["original_msg"]

    # 未超阈值：enum_values 原样内联
    assert "列枚举值：" in detail
    assert "open" in detail
    # 空列表 / None 不渲染列枚举值段
    owner_line = next(line for line in detail.splitlines() if "owner" in line)
    remark_line = next(line for line in detail.splitlines() if "remark" in line)
    assert "列枚举值" not in owner_line
    assert "列枚举值" not in remark_line
    # 不生成卸载文件
    assert not _offload_files(tmp_path)
    # data 与 .metric_dir JSON 保留完整 enum_values
    assert result["data"]["columns"][0]["enum_values"] == [{"value": "open", "description": "打开"}]
    metric_json = json.loads(
        next(tmp_path.joinpath(".metric_dir").glob("output_get_table_schema_*.json")).read_text(encoding="utf-8")
    )
    assert metric_json["columns"][0]["enum_values"] == [{"value": "open", "description": "打开"}]


def test_enum_values_offloaded_when_over_default_threshold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    big_enum_values = [{"value": f"code_{i:04d}", "description": f"状态描述_{i}"} for i in range(500)]
    cols_raw = _make_cols_raw([("status", big_enum_values), ("plain", None)])
    _install(monkeypatch, cols_raw, tmp_path)

    result = stws.get_table_schema("db.t", _tool_context=ToolExecutionContext(tool_config={}))
    detail = result["original_msg"]

    # 超出默认阈值 ENUM_VALUES_MAX_LENGTH 值：不再内联，改为路径引用
    assert "列枚举值：" not in detail
    assert f"超出阈值 {ENUM_VALUES_MAX_LENGTH}" in detail
    assert "详见文件：" in detail
    assert "文件内容说明：按列分段" in detail
    # agent_workspace 根目录生成唯一的“表名+随机数”命名 txt
    offload_files = _offload_files(tmp_path)
    assert len(offload_files) == 1
    # detail 中的路径指向该文件
    path_in_detail = Path(detail.split("详见文件：", 1)[1].splitlines()[0].strip())
    assert path_in_detail == offload_files[0].resolve()
    # 文件内容含表头与全部枚举值明细
    content = offload_files[0].read_text(encoding="utf-8")
    assert "表 db.t 列枚举值明细" in content
    assert "格式说明：按列分段" in content
    assert "[status (string)]" in content
    assert "code_0000" in content
    assert "状态描述_499" in content
    # data 仍保留完整 enum_values
    assert result["data"]["columns"][0]["enum_values"] == big_enum_values


def test_enum_values_threshold_override_via_tool_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mixed_enum_values = [{"value": "a", "description": "甲"}, "raw_b"]
    cols_raw = _make_cols_raw([("status", mixed_enum_values)])
    _install(monkeypatch, cols_raw, tmp_path)

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(tool_config={"enum_values_max_length": 32})
    )
    detail = result["original_msg"]

    # 工具级覆盖阈值生效
    assert "超出阈值 32" in detail
    assert "详见文件：" in detail
    assert "文件内容说明：按列分段" in detail
    offload_files = _offload_files(tmp_path)
    assert len(offload_files) == 1
    # dict 条目与非 dict 条目均写入文件
    content = offload_files[0].read_text(encoding="utf-8")
    assert "格式说明：按列分段" in content
    assert "  - a：甲" in content
    assert "  - raw_b" in content


def test_enum_values_invalid_override_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    small_enum_values = [{"value": "a", "description": "甲"}]
    cols_raw = _make_cols_raw([("status", small_enum_values)])
    _install(monkeypatch, cols_raw, tmp_path)

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(tool_config={"enum_values_max_length": "abc"})
    )

    # 非法配置回退默认 ENUM_VALUES_MAX_LENGTH 值，小体量枚举值保持内联且不落盘
    assert "列枚举值：" in result["original_msg"]
    assert "详见文件：" not in result["original_msg"]
    assert not _offload_files(tmp_path)


def test_enum_values_offload_falls_back_inline_without_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    big_enum_values = [{"value": f"code_{i:04d}", "description": f"状态描述_{i}"} for i in range(500)]
    cols_raw = _make_cols_raw([("status", big_enum_values)])
    _install(monkeypatch, cols_raw, tmp_path, sandbox_raises=True)

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(tool_config={"enum_values_max_length": 32})
    )
    detail = result["original_msg"]

    # 落盘失败回退内联渲染，工具不报错
    assert "列枚举值：" in detail
    assert "code_0499" in detail
    assert "详见文件：" not in detail
    assert not _offload_files(tmp_path)


def test_all_empty_enum_values_render_nothing_and_never_offload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cols_raw = _make_cols_raw([("a", []), ("b", None)])
    _install(monkeypatch, cols_raw, tmp_path)

    result = stws.get_table_schema("db.t", _tool_context=ToolExecutionContext(tool_config={}))

    # 空枚举值不计入阈值、不触发卸载、不渲染枚举值段
    assert "列枚举值" not in result["original_msg"]
    assert not _offload_files(tmp_path)


def test_enum_values_disabled_via_tool_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cols_raw = _make_cols_raw(
        [
            ("status", [{"value": "open", "description": "打开"}]),
            ("plain", None),
        ]
    )
    _install(monkeypatch, cols_raw, tmp_path)

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(tool_config={"enum_values_enabled": False})
    )
    detail = result["original_msg"]

    # 关闭开关：detail 不渲染列枚举值、不触发卸载
    assert "列枚举值" not in detail
    assert not _offload_files(tmp_path)
    # data.columns 彻底不含 enum_values key，公共字段不受开关影响
    expected_keys = {"name", "full_name", "description", "value_type", "column_properties"}
    assert set(result["data"]["columns"][0]) == expected_keys
    # .metric_dir JSON 同样不含 enum_values key
    metric_json = json.loads(
        next(tmp_path.joinpath(".metric_dir").glob("output_get_table_schema_*.json")).read_text(encoding="utf-8")
    )
    assert "enum_values" not in metric_json["columns"][0]


def test_enum_values_disabled_accepts_string_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cols_raw = _make_cols_raw([("status", [{"value": "open", "description": "打开"}])])
    _install(monkeypatch, cols_raw, tmp_path)

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(tool_config={"enum_values_enabled": "false"})
    )

    # YAML/JSON 传入字符串 "false" 同样生效
    assert "列枚举值" not in result["original_msg"]
    assert "enum_values" not in result["data"]["columns"][0]
    assert not _offload_files(tmp_path)


def test_enum_values_enabled_strict_true_false_strings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cols_raw = _make_cols_raw([("status", [{"value": "open", "description": "打开"}])])
    _install(monkeypatch, cols_raw, tmp_path)

    # 大写 "FALSE"：大小写不敏感，视为 False
    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(tool_config={"enum_values_enabled": "FALSE"})
    )
    assert "列枚举值" not in result["original_msg"]
    assert "enum_values" not in result["data"]["columns"][0]
    assert not _offload_files(tmp_path)

    # "yes" 等宽泛取值不再被接受：告警回退默认值（monkeypatch 默认 False 以区分拒绝回退与按 True 接受）
    monkeypatch.setattr(stws, "ENUM_VALUES_ENABLED", False)
    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(tool_config={"enum_values_enabled": "yes"})
    )
    assert "列枚举值" not in result["original_msg"]
    assert "enum_values" not in result["data"]["columns"][0]
    assert not _offload_files(tmp_path)


def test_enum_values_invalid_enabled_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cols_raw = _make_cols_raw([("status", [{"value": "open", "description": "打开"}])])
    _install(monkeypatch, cols_raw, tmp_path)

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(tool_config={"enum_values_enabled": 123})
    )

    # 非法配置回退默认 True，保持现行为（内联渲染、data.columns 保留）
    assert "列枚举值：" in result["original_msg"]
    assert result["data"]["columns"][0]["enum_values"] == [{"value": "open", "description": "打开"}]
    assert not _offload_files(tmp_path)


def _make_local_functions_config() -> dict[str, Any]:
    """构造子代理 temp config 的最小 TOOLS.local_functions 段。"""
    return {
        "TOOLS": {
            "local_functions": [
                {
                    "name": "get_table_schema",
                    "module": "dataagent.actions.tools.semantic_tool.search_tables_with_schema",
                    "function": "get_table_schema",
                },
                {
                    "name": "search_udf_function_by_name_keyword",
                    "module": "dataagent.actions.tools.semantic_tool.search_udf_functions",
                    "function": "search_udf_function_by_name_keyword",
                },
            ]
        }
    }


def _get_local_functions_entry(temp_config: dict[str, Any], function: str) -> dict[str, Any]:
    return next(e for e in temp_config["TOOLS"]["local_functions"] if e.get("function") == function)


def test_enum_values_config_propagated_to_get_table_schema_entry() -> None:
    temp_config = _make_local_functions_config()

    stws.inject_enum_values_config(temp_config, {"llm_model": "chat_model", "enum_values_enabled": False})

    # 仅透传 enum_values_* 键；llm_model 不落入 get_table_schema 的 config
    entry = _get_local_functions_entry(temp_config, "get_table_schema")
    assert entry["config"] == {"enum_values_enabled": False}
    other = _get_local_functions_entry(temp_config, "search_udf_function_by_name_keyword")
    assert "config" not in other


def test_enum_values_switch_and_threshold_both_propagated() -> None:
    temp_config = _make_local_functions_config()

    stws.inject_enum_values_config(temp_config, {"enum_values_enabled": True, "enum_values_max_length": 32})

    entry = _get_local_functions_entry(temp_config, "get_table_schema")
    assert entry["config"] == {"enum_values_enabled": True, "enum_values_max_length": 32}


def test_enum_values_config_merges_into_existing_entry_config() -> None:
    temp_config = _make_local_functions_config()
    entry = _get_local_functions_entry(temp_config, "get_table_schema")
    entry["config"] = {"llm_model": "chat_model"}

    stws.inject_enum_values_config(temp_config, {"enum_values_enabled": False})

    assert entry["config"] == {"llm_model": "chat_model", "enum_values_enabled": False}


def test_enum_values_config_no_propagation_when_keys_absent() -> None:
    temp_config = _make_local_functions_config()

    stws.inject_enum_values_config(temp_config, {"llm_model": "chat_model"})
    stws.inject_enum_values_config(temp_config, None)

    entry = _get_local_functions_entry(temp_config, "get_table_schema")
    assert "config" not in entry


class _FakeToolsConfigManager:
    """模拟主进程 config_manager：返回合并后的 TOOLS.local_functions。"""

    def __init__(self, local_functions: list[dict[str, Any]]) -> None:
        self._local_functions = local_functions

    def get(self, key: str, default: Any = None) -> Any:
        if key == "TOOLS":
            return {"local_functions": self._local_functions}
        return default


def _mr_entry(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "metadata_recall",
        "module": "dataagent.actions.tools.semantic_tool.metadata_recall",
        "function": "metadata_recall",
        "config": config,
    }


def test_enum_values_fallback_disables_field_via_metadata_recall_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 模拟 wrapper/check_table_existence 主进程直调：tool_config 无开关，经 config_manager 回退
    cols_raw = _make_cols_raw([("status", [{"value": "open", "description": "打开"}])])
    _install(monkeypatch, cols_raw, tmp_path)
    config_manager = _FakeToolsConfigManager(
        [_mr_entry({"llm_model": "chat_model", "enum_values_enabled": False})]
    )

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(config_manager=config_manager, tool_config={})
    )

    assert "列枚举值" not in result["original_msg"]
    assert "enum_values" not in result["data"]["columns"][0]
    assert not _offload_files(tmp_path)


def test_enum_values_fallback_defaults_when_metadata_recall_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # agent 未注册 metadata_recall：回退读不到，走模块默认 True
    cols_raw = _make_cols_raw([("status", [{"value": "open", "description": "打开"}])])
    _install(monkeypatch, cols_raw, tmp_path)
    config_manager = _FakeToolsConfigManager([])

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(config_manager=config_manager, tool_config={})
    )

    assert "列枚举值：" in result["original_msg"]
    assert result["data"]["columns"][0]["enum_values"] == [{"value": "open", "description": "打开"}]


def test_enum_values_tool_config_overrides_metadata_recall_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 工具级 tool_config 优先于 metadata_recall 条目回退
    cols_raw = _make_cols_raw([("status", [{"value": "open", "description": "打开"}])])
    _install(monkeypatch, cols_raw, tmp_path)
    config_manager = _FakeToolsConfigManager([_mr_entry({"enum_values_enabled": False})])

    result = stws.get_table_schema(
        "db.t",
        _tool_context=ToolExecutionContext(
            config_manager=config_manager, tool_config={"enum_values_enabled": True}
        ),
    )

    assert "列枚举值：" in result["original_msg"]
    assert result["data"]["columns"][0]["enum_values"] == [{"value": "open", "description": "打开"}]


def test_enum_values_fallback_last_match_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 多条 metadata_recall 条目时取最后一条，镜像同名后注册者胜的注册语义
    cols_raw = _make_cols_raw([("status", [{"value": "open", "description": "打开"}])])
    _install(monkeypatch, cols_raw, tmp_path)
    config_manager = _FakeToolsConfigManager(
        [
            _mr_entry({"enum_values_enabled": True}),
            _mr_entry({"enum_values_enabled": False}),
        ]
    )

    result = stws.get_table_schema(
        "db.t", _tool_context=ToolExecutionContext(config_manager=config_manager, tool_config={})
    )

    assert "列枚举值" not in result["original_msg"]
    assert "enum_values" not in result["data"]["columns"][0]
