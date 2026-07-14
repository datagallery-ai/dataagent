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
"""
Unit tests for ResultIRConverter.

测试组织原则：
- 按两条Pipeline分组：内容Pipeline（Content Pipeline）和文件Pipeline（File Pipeline）
- 内容Pipeline按子步骤测试：DataFrame, columns+data, inline script, structured IR, knowledge
- 文件Pipeline测试：workspace 快照差集检测新增/修改文件，按扩展名分类
- Combined 测试多Pipeline交叉场景
"""

import time
from pathlib import Path
from typing import cast

import pytest

from dataagent.core.context.context import Context, ContextFactory
from dataagent.core.context.context_ir import ColumnNode, FileNode, ScriptNode, TableNode
from dataagent.utils.converter.result_ir_converter import ResultIRConverter


@pytest.fixture(autouse=True)
def _clear_context_factory():
    """Ensure each test starts with a clean ContextFactory."""
    ContextFactory.clear_context()
    yield
    ContextFactory.clear_context()


@pytest.fixture()
def context() -> Context:
    """Create a Context with a registered ActionNode as IR predecessor."""
    ctx = ContextFactory.get_context(
        user_id="test_user",
        session_id="test_session",
        run_id=0,
        sub_id=0,
    )
    ctx.register_query(query="test query", additional_files=[])
    ctx.register_node(
        node_type="Action",
        label="test_action_001",
        description="test action",
        predecessor_node=["Query(query00000)"],
        action="some_tool",
        params={"key": "value"},
        output="Pending",
        success=False,
    )
    return ctx


ACTION_LABEL = "Action(test_action_001)"


def _labels_of(created: list[str], prefix: str) -> list[str]:
    """Filter created labels by IR type prefix."""
    return [lbl for lbl in created if lbl.startswith(f"{prefix}(")]


class TestTableDetection:
    """内容Pipeline中 TableNode 的检测。"""

    def test_from_columns_data_creates_table_and_columns(self, context: Context):
        """columns+data 内存表格模式 → TableNode + ColumnNode。"""
        result = {
            "columns": ["a", "b"],
            "data": [{"a": 1, "b": 2}],
        }
        created = ResultIRConverter.convert(
            context=context,
            tool_name="sql_tool",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
        )

        tables = _labels_of(created, "Table")
        columns = _labels_of(created, "Column")
        assert len(tables) >= 1
        assert cast(TableNode, context.get_IR_from_node(graph_node_label=tables[0])).path == ""
        assert len(columns) == 2
        for col in columns:
            preds = list(context.get_trajectory().predecessors(col))
            assert preds[0] == tables[0]

    def test_from_columns_data_without_data_rows(self, context: Context):
        """columns+data 但无数据行 → TableNode(path='')，不产生 Column 采样。"""
        result = {"columns": ["x"], "data": []}
        created = ResultIRConverter.convert(
            context=context,
            tool_name="sql_tool",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
        )

        tables = _labels_of(created, "Table")
        assert len(tables) == 1
        assert cast(TableNode, context.get_IR_from_node(graph_node_label=tables[0])).path == ""

    def test_no_table_when_no_indicators(self, context: Context):
        """反例: result 无 columns/data → 不产生 TableNode。"""
        result = {"status": "ok", "message": "nothing"}
        created = ResultIRConverter.convert(
            context=context,
            tool_name="some_tool",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
        )
        assert _labels_of(created, "Table") == []

    def test_from_dataframe(self, context: Context):
        """result 中的 pd.DataFrame → 持久化为 CSV → TableNode + ColumnNode。"""
        import pandas as pd  # pyright: ignore[reportMissingTypeStubs]

        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        created = ResultIRConverter.convert(
            context=context,
            tool_name="analysis",
            tool_call_id="test_action_001",
            tool_args={},
            result={"data": df},
            action_node_label=ACTION_LABEL,
        )

        tables = _labels_of(created, "Table")
        assert len(tables) >= 1
        assert cast(TableNode, context.get_IR_from_node(graph_node_label=tables[0])).path.endswith(".csv")

    def test_from_dataframe_persists_to_workspace(self, context: Context, tmp_path: Path):
        """workspace 存在时 DataFrame 持久化到 workspace 目录内。"""
        import pandas as pd  # pyright: ignore[reportMissingTypeStubs]

        df = pd.DataFrame({"x": [1]})
        created = ResultIRConverter.convert(
            context=context,
            tool_name="analysis",
            tool_call_id="test_action_001",
            tool_args={},
            result=df,
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files={},
        )

        tables = _labels_of(created, "Table")
        assert len(tables) >= 1
        table_path = cast(TableNode, context.get_IR_from_node(graph_node_label=tables[0])).path
        assert table_path.startswith(str(tmp_path))
        assert Path(table_path).exists()


class TestScriptDetection:
    """内容Pipeline中从 tool_args 提取内联脚本。"""

    def test_from_sql_arg(self, context: Context):
        """入参 key='sql' → ScriptNode(script_type='sql')。"""
        sql = "SELECT * FROM users WHERE age > 18"
        created = ResultIRConverter.convert(
            context=context,
            tool_name="sql_tool",
            tool_call_id="test_action_001",
            tool_args={"sql": sql},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
        )
        scripts = _labels_of(created, "Script")
        assert len(scripts) == 1
        ir = cast(ScriptNode, context.get_IR_from_node(graph_node_label=scripts[0]))
        assert ir.script_content == sql
        assert ir.script_type == "sql"

    def test_from_code_arg(self, context: Context):
        """入参 key='code' → ScriptNode(script_type='python')。"""
        code = "import pandas as pd\ndf = pd.read_csv('data.csv')"
        created = ResultIRConverter.convert(
            context=context,
            tool_name="runner",
            tool_call_id="test_action_001",
            tool_args={"code": code},
            result="OK",
            action_node_label=ACTION_LABEL,
        )
        scripts = _labels_of(created, "Script")
        assert len(scripts) == 1
        assert cast(ScriptNode, context.get_IR_from_node(graph_node_label=scripts[0])).script_type == "python"

    def test_empty_arg_creates_nothing(self, context: Context):
        """反例: 空字符串 → 不创建 ScriptNode。"""
        created = ResultIRConverter.convert(
            context=context,
            tool_name="sql_tool",
            tool_call_id="test_action_001",
            tool_args={"sql": ""},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
        )
        assert _labels_of(created, "Script") == []


class TestDataFieldDetection:
    """内容Pipeline中结构化 IR 条目的检测，兼容 Executor unwrap。"""

    def test_table_entries_create_table_nodes(self, context: Context):
        """result 中的 table 列表 → TableNode。"""
        result = {
            "original_msg": "Found 2 tables.",
            "frontend_msg": "Found 2 tables.",
            "data": {
                "table": [
                    {"label": "db.orders", "description": "订单表", "path": "db.orders"},
                    {"label": "db.users", "description": "用户表", "path": "db.users"},
                ],
            },
        }
        created = ResultIRConverter.convert(
            context=context,
            tool_name="perceive_metadata",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
        )
        tables = _labels_of(created, "Table")
        assert len(tables) == 2
        ir0 = cast(TableNode, context.get_IR_from_node(graph_node_label=tables[0]))
        assert ir0.path in ("db.orders", "db.users")

    def test_table_entries_at_current_level(self, context: Context):
        """Executor unwrap 后 table/column/tool 已在当前层 → 直接识别。"""
        result = {
            "table": [
                {"label": "db.orders", "description": "订单表", "path": "db.orders"},
            ],
        }
        created = ResultIRConverter.convert(
            context=context,
            tool_name="perceive_metadata",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
        )
        tables = _labels_of(created, "Table")
        assert len(tables) == 1

    def test_column_entries_create_column_nodes(self, context: Context):
        """column 列表 → ColumnNode，挂载于对应 TableNode 下。"""
        result = {
            "original_msg": "Found 1 column.",
            "frontend_msg": "Found 1 column.",
            "data": {
                "table": [
                    {"label": "db.orders", "description": "订单表", "path": "db.orders"},
                ],
                "column": [
                    {
                        "label": "db.orders.amount",
                        "description": "金额",
                        "from_table": "db.orders",
                        "values": {"min": 0, "max": 999},
                        "supplementary_schemas": [],
                    },
                ],
            },
        }
        created = ResultIRConverter.convert(
            context=context,
            tool_name="perceive_metadata",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
        )
        columns = _labels_of(created, "Column")
        tables = _labels_of(created, "Table")
        assert len(columns) == 1
        assert len(tables) == 1
        col_ir = cast(ColumnNode, context.get_IR_from_node(graph_node_label=columns[0]))
        assert col_ir.from_table == "db.orders"
        preds = list(context.get_trajectory().predecessors(columns[0]))
        assert len(preds) == 1
        assert preds[0] == tables[0]

    def test_data_field_suppresses_knowledge_fallback(self, context: Context):
        """结构化 IR 成功创建后，不应退化为 KnowledgeNode。"""
        result = {
            "original_msg": "Found tables. " * 100,
            "frontend_msg": "Found tables. " * 100,
            "data": {
                "table": [
                    {"label": "db.t1", "description": "t1", "path": "db.t1"},
                ],
            },
        }
        created = ResultIRConverter.convert(
            context=context,
            tool_name="perceive",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
        )
        assert len(_labels_of(created, "Table")) == 1
        assert _labels_of(created, "Knowledge") == []


class TestKnowledgeFallback:
    """内容Pipeline中 长文本落盘 的触发与抑制。"""

    def test_long_text_creates_file_node(self, context: Context, tmp_path: Path):
        """长文本 result → FileNode（落盘到 workspace）。"""
        long_text = "Detailed analysis. " * 100
        created = ResultIRConverter.convert(
            context=context,
            tool_name="analysis",
            tool_call_id="test_action_001",
            tool_args={},
            result=long_text,
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
        )
        fn = _labels_of(created, "File")
        assert len(fn) == 1
        node = cast(FileNode, context.get_IR_from_node(graph_node_label=fn[0]))
        assert node.source == "analysis"
        assert Path(node.path).exists()

    def test_short_text_creates_nothing(self, context: Context):
        """短文本 result → 不建 IR。"""
        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result="OK",
            action_node_label=ACTION_LABEL,
        )
        assert len(created) == 0

    def test_fallback_ignores_frontend_and_data_when_original_msg_short(
        self,
        context: Context,
        tmp_path: Path,
    ):
        """标准工具返回中只有 original_msg 参与长文本 File fallback 阈值检测。"""
        result = {
            "original_msg": "OK",
            "frontend_msg": "frontend " * 100,
            "data": {"payload": "data " * 100},
        }
        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            knowledge_min_length=20,
        )

        assert _labels_of(created, "File") == []

    def test_fallback_uses_visible_result_when_original_msg_absent(
        self,
        context: Context,
        tmp_path: Path,
    ):
        """Executor 真实路径：result 已是 data 部分（无 original_msg 外壳），
        但 visible_result（模型可见文本）短 → 不触发落盘，data 不参与阈值检测。"""
        result = {"payload": "data " * 100}  # executor 传入的 data 部分
        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            knowledge_min_length=20,
            visible_result="short visible",  # = original_msg or output_text
        )

        assert _labels_of(created, "File") == []

    def test_fallback_serializes_original_msg_without_full_result(
        self,
        context: Context,
        tmp_path: Path,
    ):
        """非字符串 original_msg 超过阈值时，只落盘 original_msg 内容。"""
        result = {
            "original_msg": {"summary": "x" * 40},
            "frontend_msg": "frontend " * 100,
            "data": {"payload": "data " * 100},
        }
        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result=result,
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            knowledge_min_length=20,
        )

        file_labels = _labels_of(created, "File")
        assert len(file_labels) == 1
        node = cast(FileNode, context.get_IR_from_node(graph_node_label=file_labels[0]))
        persisted = Path(node.path).read_text(encoding="utf-8")
        assert "summary" in persisted
        assert "frontend_msg" not in persisted
        assert "payload" not in persisted

    def test_suppressed_when_file_pipeline_creates_ir(self, context: Context, tmp_path: Path):
        """文件Pipeline已创建 IR → 不建 KnowledgeNode（注意 knowledge 只在内容Pipeline无产出时触发）。"""
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))
        (tmp_path / "report.md").write_text("# " + "Long content " * 100)

        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )
        assert len(_labels_of(created, "File")) == 1
        assert _labels_of(created, "Knowledge") == []


class TestFilePipeline:
    """文件Pipeline：通过 workspace 快照差集检测新增/修改文件。"""

    def test_new_csv_creates_table_node(self, context: Context, tmp_path: Path):
        """workspace 中新增 .csv → TableNode。"""
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))
        (tmp_path / "data.csv").write_text("a,b\n1,2\n")

        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )
        assert len(_labels_of(created, "Table")) >= 1

    def test_new_script_file_creates_script_node(self, context: Context, tmp_path: Path):
        """workspace 中新增 .sql → ScriptNode（非 FileNode）。"""
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))
        (tmp_path / "query.sql").write_text("SELECT 1;")

        created = ResultIRConverter.convert(
            context=context,
            tool_name="gen_sql",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )
        assert len(_labels_of(created, "Script")) == 1
        assert _labels_of(created, "File") == []
        ir = cast(ScriptNode, context.get_IR_from_node(graph_node_label=_labels_of(created, "Script")[0]))
        assert ir.script_type == "sql"

    def test_new_generic_files_create_file_nodes(self, context: Context, tmp_path: Path):
        """workspace 中新增非表格/脚本文件 → FileNode。"""
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))
        (tmp_path / "report.md").write_text("# New")
        (tmp_path / "chart.png").write_bytes(b"\x89PNG")

        created = ResultIRConverter.convert(
            context=context,
            tool_name="batch",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )
        assert len(_labels_of(created, "File")) == 2

    def test_modified_file_detected(self, context: Context, tmp_path: Path):
        """已有文件被修改（mtime 变化）→ 文件Pipeline检测到。"""
        f = tmp_path / "data.txt"
        f.write_text("original")
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))

        time.sleep(0.05)
        f.write_text("modified content")

        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )
        assert len(_labels_of(created, "File")) == 1

    def test_unchanged_file_ignored(self, context: Context, tmp_path: Path):
        """未修改的已有文件 → 不产生 IR。"""
        (tmp_path / "old.txt").write_text("unchanged")
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))

        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )
        assert len(created) == 0

    def test_subdirectory_files_detected(self, context: Context, tmp_path: Path):
        """递归扫描：子目录中的新增文件也能被检测。"""
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))
        sub = tmp_path / "subdir" / "nested"
        sub.mkdir(parents=True)
        (sub / "deep.csv").write_text("a\n1\n")

        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )
        assert len(_labels_of(created, "Table")) == 1

    def test_no_workspace_returns_empty(self, context: Context):
        """无 workspace → 文件Pipeline不运行。"""
        created = ResultIRConverter.convert(
            context=context,
            tool_name="tool",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
        )
        assert len(created) == 0


class TestUtilityMethods:
    """snapshot_dir 相关测试。"""

    def test_snapshot_dir_returns_dict_with_mtime(self, tmp_path: Path):
        """返回 {路径: mtime} 映射。"""
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        (tmp_path / "subdir").mkdir()

        snap = ResultIRConverter.snapshot_dir(str(tmp_path))
        assert isinstance(snap, dict)
        assert len(snap) == 2
        for path, mtime in snap.items():
            assert isinstance(mtime, float)
            assert Path(path).exists()

    def test_snapshot_dir_recursive(self, tmp_path: Path):
        """递归扫描子目录中的文件。"""
        (tmp_path / "top.txt").write_text("top")
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        (sub / "deep.txt").write_text("deep")

        snap = ResultIRConverter.snapshot_dir(str(tmp_path))
        assert len(snap) == 2

    def test_snapshot_dir_empty_returns_empty(self):
        """空目录 / None → 空 dict。"""
        assert ResultIRConverter.snapshot_dir(None) == {}
        assert ResultIRConverter.snapshot_dir("/nonexistent/path") == {}

    def test_snapshot_dir_includes_files_under_dot_home_prefix(self, tmp_path: Path):
        """绝对路径含 .dataagent_home 时，仍应快照到 workspace 内用户文件。"""
        workspace = tmp_path / ".dataagent_home" / "anonymous" / "session"
        workspace.mkdir(parents=True)
        csv_path = workspace / "sample.csv"
        csv_path.write_text("a,b\n1,2\n", encoding="utf-8")

        snap = ResultIRConverter.snapshot_dir(str(workspace))
        assert str(csv_path.resolve()) in snap

    def test_snapshot_dir_skips_workspace_internal_hidden_dirs(self, tmp_path: Path):
        """workspace 内 .context / .dataagent 等隐藏目录仍应跳过。"""
        workspace = tmp_path / ".dataagent_home" / "sess"
        workspace.mkdir(parents=True)
        (workspace / "keep.txt").write_text("ok", encoding="utf-8")
        context_dir = workspace / ".context"
        context_dir.mkdir()
        (context_dir / "Run0.json").write_text("{}", encoding="utf-8")
        tool_out = workspace / ".dataagent" / "tool_outputs"
        tool_out.mkdir(parents=True)
        (tool_out / "out.txt").write_text("skip", encoding="utf-8")

        snap = ResultIRConverter.snapshot_dir(str(workspace))
        assert str((workspace / "keep.txt").resolve()) in snap
        assert not any(".context" in p for p in snap)
        assert not any("tool_outputs" in p for p in snap)

    def test_new_csv_under_dot_home_workspace_creates_table(self, context: Context, tmp_path: Path):
        """默认 DATAAGENT_HOME 风格路径下新增 .csv → TableNode（非 FileNode）。"""
        workspace = tmp_path / ".dataagent_home" / "anonymous" / "session"
        workspace.mkdir(parents=True)
        pre = ResultIRConverter.snapshot_dir(str(workspace))
        csv_path = workspace / "sample.csv"
        csv_path.write_text("name,age\nAlice,28\n", encoding="utf-8")

        created = ResultIRConverter.convert(
            context=context,
            tool_name="write_file",
            tool_call_id="test_action_001",
            tool_args={"path": str(csv_path.resolve()), "content": "name,age\nAlice,28\n"},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(workspace),
            pre_existing_files=pre,
        )
        assert len(_labels_of(created, "Table")) == 1
        assert _labels_of(created, "File") == []

    def test_write_file_arg_path_csv_creates_table_without_snapshot(self, context: Context, tmp_path: Path):
        """无快照差集时，write_file 参数中的 .csv 路径仍应按扩展名建成 TableNode。"""
        csv_path = tmp_path / "demo.csv"
        csv_path.write_text("a,b\n1,2\n", encoding="utf-8")
        resolved = str(csv_path.resolve())

        created = ResultIRConverter.convert(
            context=context,
            tool_name="write_file",
            tool_call_id="test_action_001",
            tool_args={"path": resolved, "content": "a,b\n1,2\n"},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=None,
        )
        assert len(_labels_of(created, "Table")) == 1
        assert _labels_of(created, "File") == []
        ir = cast(TableNode, context.get_IR_from_node(graph_node_label=_labels_of(created, "Table")[0]))
        assert ir.path == resolved


class TestCombinedScenarios:
    """测试多Pipeline交叉的真实场景。"""

    def test_sql_arg_with_table_result(self, context: Context):
        """sql 入参 + columns/data 结果 → 同时产生 TableNode + ScriptNode。"""
        created = ResultIRConverter.convert(
            context=context,
            tool_name="sql_tool",
            tool_call_id="test_action_001",
            tool_args={"sql": "SELECT x, y FROM t"},
            result={"columns": ["x", "y"], "data": [{"x": 1, "y": 2}]},
            action_node_label=ACTION_LABEL,
        )
        assert len(_labels_of(created, "Table")) >= 1
        assert len(_labels_of(created, "Script")) >= 1

    def test_mixed_files_in_workspace(self, context: Context, tmp_path: Path):
        """workspace 中同时出现 .csv + .py + .html → TableNode + ScriptNode + FileNode。"""
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))
        (tmp_path / "data.csv").write_text("a\n1\n")
        (tmp_path / "transform.py").write_text("print('hello')")
        (tmp_path / "report.html").write_text("<html></html>")

        created = ResultIRConverter.convert(
            context=context,
            tool_name="pipeline",
            tool_call_id="test_action_001",
            tool_args={},
            result={"status": "ok"},
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )

        assert len(_labels_of(created, "Table")) >= 1
        assert len(_labels_of(created, "Script")) >= 1
        assert len(_labels_of(created, "File")) >= 1

    def test_dataframe_persisted_to_workspace_not_duplicated(self, context: Context, tmp_path: Path):
        """DataFrame 持久化到 workspace → 内容Pipeline建 Table，文件Pipeline跳过该 CSV。"""
        import pandas as pd  # pyright: ignore[reportMissingTypeStubs]

        df = pd.DataFrame({"a": [1]})
        pre = ResultIRConverter.snapshot_dir(str(tmp_path))

        created = ResultIRConverter.convert(
            context=context,
            tool_name="analysis",
            tool_call_id="test_action_001",
            tool_args={},
            result=df,
            action_node_label=ACTION_LABEL,
            workspace=str(tmp_path),
            pre_existing_files=pre,
        )

        tables = _labels_of(created, "Table")
        assert len(tables) == 1


class TestReadFilePipeline:
    """Pipeline 3：read_file 去重与 refers_to 边。"""

    def test_read_file_creates_file_node_on_first_read(self, context: Context, tmp_path: Path):
        """首次 read_file → 注册 FileNode。"""
        f = tmp_path / "notes.txt"
        f.write_text("hello world", encoding="utf-8")
        resolved = str(f.resolve())

        created = ResultIRConverter.convert(
            context=context,
            tool_name="read_file",
            tool_call_id="test_action_001",
            tool_args={"path": resolved},
            result="hello world",
            action_node_label=ACTION_LABEL,
        )
        file_labels = _labels_of(created, "File")
        assert len(file_labels) == 1
        ir = cast(FileNode, context.get_IR_from_node(graph_node_label=file_labels[0]))
        assert ir.path == resolved

    def test_read_file_existing_file_adds_refers_to_only(self, context: Context, tmp_path: Path):
        """再次 read_file 同一文件（MD5 不变）→ 仅 refers_to，不新建节点。"""
        f = tmp_path / "notes.txt"
        f.write_text("hello world", encoding="utf-8")
        resolved = str(f.resolve())

        first = ResultIRConverter.convert(
            context=context,
            tool_name="read_file",
            tool_call_id="test_action_001",
            tool_args={"path": resolved},
            result="hello world",
            action_node_label=ACTION_LABEL,
        )
        file_label = _labels_of(first, "File")[0]

        context.register_node(
            node_type="Action",
            label="read_again",
            description="second read",
            predecessor_node=["Query(query00000)"],
            edge_type="triggers",
            action="read_file",
            params={"path": resolved},
            output="hello world",
            success=True,
            add_pt=True,
        )
        second_action = "Action(read_again)"

        created = ResultIRConverter.convert(
            context=context,
            tool_name="read_file",
            tool_call_id="read_again",
            tool_args={"path": resolved},
            result="hello world",
            action_node_label=second_action,
        )
        assert len(created) == 0

        traj = context.get_trajectory()
        assert traj.has_edge(second_action, file_label)
        assert traj.get_edge_data(second_action, file_label, {}).get("edge_type") == "refers_to"

    def test_read_file_modified_content_creates_new_node(self, context: Context, tmp_path: Path):
        """同路径但内容变更（MD5 不同）→ 注册新版本 FileNode。"""
        f = tmp_path / "notes.txt"
        f.write_text("version one", encoding="utf-8")
        resolved = str(f.resolve())
        context.state.workspace = str(tmp_path.resolve())

        ResultIRConverter.convert(
            context=context,
            tool_name="read_file",
            tool_call_id="test_action_001",
            tool_args={"path": resolved},
            result="version one",
            action_node_label=ACTION_LABEL,
        )

        f.write_text("version two", encoding="utf-8")
        context.register_node(
            node_type="Action",
            label="read_modified",
            description="read after modify",
            predecessor_node=["Query(query00000)"],
            edge_type="triggers",
            action="read_file",
            params={"path": resolved},
            output="version two",
            success=True,
            add_pt=True,
        )

        created = ResultIRConverter.convert(
            context=context,
            tool_name="read_file",
            tool_call_id="read_modified",
            tool_args={"path": resolved},
            result="version two",
            action_node_label="Action(read_modified)",
        )
        assert len(_labels_of(created, "File")) == 1
