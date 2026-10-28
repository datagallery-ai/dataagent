# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for the ``dataops_quality_check_post`` agent post-hook.

The hook trial-runs the delivered INSERT at turn end and appends a report.
The contracts under test:

1. Skip guards — incomplete turn, no workspace, no deliverable → state untouched.
2. Run guard — the same ``run_id`` is never trial-computed twice.
3. Reporting — warnings are surfaced as text and never fail the turn.
4. Failures never propagate out of the hook.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from dataagent.core.suite.builtin_suites.de_agent.hooks import dataops_quality_check_post as hook


def _runtime(workspace: Path | None) -> MagicMock:
    runtime = MagicMock()
    runtime.workspace_dir = workspace
    runtime.get_cache.return_value = set()
    return runtime


def _state(*, complete: bool = True, run_id: int = 1) -> dict:
    return {"complete": complete, "run_id": run_id, "messages": []}


def _deliverable(workspace: Path, name: str = "insert_x.sql", sql: str = "INSERT OVERWRITE TABLE biads.x SELECT 1") -> Path:
    path = workspace / name
    path.write_text(sql, encoding="utf-8")
    return path


# ============================================================================
# Skip guards
# ============================================================================
class TestSkipGuards:
    def test_incomplete_turn_is_skipped(self, tmp_path):
        runtime = _runtime(tmp_path)
        result = hook.dataops_quality_check_post(_state(complete=False), runtime)
        assert result["messages"] == []

    def test_no_workspace_is_skipped(self):
        runtime = _runtime(None)
        result = hook.dataops_quality_check_post(_state(), runtime)
        assert result["messages"] == []

    def test_non_path_workspace_is_skipped(self):
        """A test-double workspace must not be treated as a real directory.

        ``MagicMock`` exposes a truthy, path-like ``workspace_dir`` whose
        ``__fspath__`` yields a junk relative path. Accepting it would make the
        hook scan a bogus directory.
        """
        runtime = MagicMock()
        runtime.get_cache.return_value = set()
        result = hook.dataops_quality_check_post(_state(), runtime)
        assert result["messages"] == []

    def test_no_deliverable_is_skipped(self, tmp_path):
        (tmp_path / "readme.md").write_text("hi", encoding="utf-8")
        runtime = _runtime(tmp_path)
        result = hook.dataops_quality_check_post(_state(), runtime)
        assert result["messages"] == []


# ============================================================================
# Deliverable discovery
# ============================================================================
class TestFindDml:
    def test_finds_insert_in_root(self, tmp_path):
        path = _deliverable(tmp_path)
        assert hook._find_dml(tmp_path) == path

    def test_prefers_the_freshest_insert(self, tmp_path):
        """取**最新修改**的 insert_*.sql，而不是固定 sql/ 优先。

        本 hook 挂在节点级（planner post），先于 agent 级的 organize_workspace
        执行 —— 此刻 sql/ 里可能是上一轮留下的旧副本。若按目录优先级取第一个，
        会试算上一轮的 SQL。
        """
        import os
        import time

        root_path = _deliverable(tmp_path, "insert_root.sql")
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        sql_path = _deliverable(sql_dir, "insert_sql.sql")

        # sql/ 里的旧副本时间戳更早 → 应该选中根目录里本轮新写的那个
        old = time.time() - 3600
        os.utime(sql_path, (old, old))

        assert hook._find_dml(tmp_path) == root_path

    def test_prefers_sql_subdirectory_when_newer(self, tmp_path):
        """sql/ 里的副本更新时，选它。"""
        import os
        import time

        root_path = _deliverable(tmp_path, "insert_root.sql")
        sql_dir = tmp_path / "sql"
        sql_dir.mkdir()
        sql_path = _deliverable(sql_dir, "insert_sql.sql")

        old = time.time() - 3600
        os.utime(root_path, (old, old))

        assert hook._find_dml(tmp_path) == sql_path
        assert hook._find_dml(tmp_path) != root_path

    def test_ignores_non_insert_files(self, tmp_path):
        (tmp_path / "create_x.sql").write_text("CREATE TABLE t (a int)", encoding="utf-8")
        assert hook._find_dml(tmp_path) is None

    def test_guess_target_table(self, tmp_path):
        path = _deliverable(
            tmp_path, "insert_x.sql", "CREATE TABLE `biads`.`x` (a int)"
        )
        assert hook._guess_target_table(path) == "biads.x"

    def test_guess_target_table_missing(self, tmp_path):
        path = _deliverable(tmp_path, "insert_x.sql", "INSERT OVERWRITE TABLE biads.x SELECT 1")
        assert hook._guess_target_table(path) == ""


# ============================================================================
# Run guard
# ============================================================================
class TestRunGuard:
    def test_same_run_id_runs_only_once(self, tmp_path):
        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)
        # Cache reflects the run guard having already fired for run_id=1.
        runtime.get_cache.return_value = {1}

        with patch.object(hook, "_invoke_trial_compute") as invoke:
            result = hook.dataops_quality_check_post(_state(run_id=1), runtime)

        assert not invoke.called
        assert result["messages"] == []

    def test_guard_is_marked_before_running(self, tmp_path):
        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)
        runtime.get_cache.return_value = set()

        with patch.object(hook, "_invoke_trial_compute", return_value={"passed": True}):
            hook.dataops_quality_check_post(_state(run_id=7), runtime)

        runtime.set_cache.assert_called_once()
        key, value = runtime.set_cache.call_args[0]
        assert key == hook._TRIAL_COMPUTE_DONE_KEY
        assert 7 in value


# ============================================================================
# Reporting
# ============================================================================
class TestRenderReport:
    def test_empty_result_renders_nothing(self):
        assert hook._render_report(dml_path=Path("x.sql"), result=None) == ""

    def test_skipped_result(self):
        report = hook._render_report(
            dml_path=Path("x.sql"), result={"skipped": True, "reason": "no_dml_path"}
        )
        assert hook._REPORT_PREFIX in report
        assert "no_dml_path" in report

    def test_failed_result_includes_diagnostics(self):
        report = hook._render_report(
            dml_path=Path("x.sql"),
            result={
                "passed": False,
                "summary": "syntax error",
                "log_analysis": {
                    "error_type": "AnalysisException",
                    "suggestions": ["add a join", "fix alias"],
                },
            },
        )
        assert "试算失败" in report
        assert "syntax error" in report
        assert "AnalysisException" in report
        assert "add a join" in report

    def test_passed_without_quality_check(self):
        report = hook._render_report(dml_path=Path("x.sql"), result={"passed": True})
        assert "试算通过" in report
        assert "未执行数据正确性校验" in report

    def test_passed_with_quality_ok(self):
        report = hook._render_report(
            dml_path=Path("x.sql"),
            result={
                "passed": True,
                "quality_check": {
                    "ok": True,
                    "checks": ["null_rate"],
                    "sample_size": 10000,
                },
            },
        )
        assert "试算通过" in report
        assert "null_rate" in report

    def test_passed_with_quality_warnings(self):
        report = hook._render_report(
            dml_path=Path("x.sql"),
            result={
                "passed": True,
                "quality_check": {
                    "ok": False,
                    "warnings": ["字段 'col1' 非空率 0.4500 < 阈值 0.80"],
                },
            },
        )
        assert "试算通过" in report
        assert "1 处告警" in report
        assert "col1" in report

    def test_quality_not_run_is_not_reported_as_pass(self):
        """校验没跑起来时，不能报「全部通过」，也不能报「0 处告警」。"""
        report = hook._render_report(
            dml_path=Path("x.sql"),
            result={
                "passed": True,
                "quality_check": {
                    "ok": False,
                    "sampled": False,
                    "error": "partition condition not found",
                    "warnings": [],
                },
            },
        )
        assert "全部通过" not in report
        assert "0 处告警" not in report
        assert "未能执行" in report
        assert "partition condition not found" in report

    def test_partial_result_shows_computed_metrics_and_failed_checks(self):
        """部分完成：跑出来的指标要展示，失败的校验项要单独说明。

        回归防护：早先「一条 checker 挂了」会让整个校验被判成「未执行」，
        已经算出来的指标全丢 —— 前端只看到一句「未能执行」，看不到任何结果。
        """
        report = hook._render_report(
            dml_path=Path("x.sql"),
            result={
                "passed": True,
                "quality_check": {
                    "ok": False,
                    "sampled": True,
                    "partial": True,
                    "checks": ["null_rate", "single_value_ratio"],
                    "completed_checks": ["null_rate"],
                    "failed_checks": [
                        {"name": "single_value_ratio", "status": "timed_out", "reason": "status='timed_out'"}
                    ],
                    "failed_metrics": {"pay_cnt_udid": ["single_value_ratio"]},
                    "sample_size": 10000,
                    "warnings": [],
                    "details": {
                        "pay_cnt_udid": {
                            "null_rate": 0.95,
                            "single_value_ratio": None,
                            "outlier_ratio": None,
                        },
                    },
                },
            },
        )
        # 已完成的校验项 + 已算出来的指标都在
        assert "部分完成" in report
        assert "null_rate" in report
        assert "0.9500" in report
        # 没算出来的指标标成「未完成」，不是「—」
        assert "未完成" in report
        # 失败的校验项和原因单独列出
        assert "未完成的校验项" in report
        assert "single_value_ratio" in report
        assert "timed_out" in report
        # 不能报成「全部通过」
        assert "全部通过" not in report


# ============================================================================
# Markdown table rendering
# ============================================================================
class TestMarkdownTable:
    def _quality(self, **overrides):
        quality = {
            "ok": True,
            "checks": ["null_rate", "single_value_ratio", "outlier_ratio"],
            "sample_size": 10000,
            "warnings": [],
            "details": {
                "pay_cnt_udid": {
                    "null_rate": 0.95,
                    "single_value_ratio": 0.12,
                    "outlier_ratio": None,
                },
            },
        }
        quality.update(overrides)
        return {"passed": True, "quality_check": quality}

    def test_metrics_are_rendered_as_a_markdown_table(self):
        report = hook._render_report(dml_path=Path("x.sql"), result=self._quality())

        assert "| 字段名 | 非空率(覆盖率) | 单一值比率 | 异常值占比 | 状态 |" in report
        assert "|---|---|---|---|---|" in report
        assert "| pay_cnt_udid | 0.9500 | 0.1200 | — | ✅ 通过 |" in report

    def test_not_applicable_metric_renders_as_dash(self):
        report = hook._render_report(dml_path=Path("x.sql"), result=self._quality())

        # 非比率类字段的异常值占比是 None —— 不能渲染成 0
        assert "| 0.0000 |" not in report

    def test_warnings_produce_a_table_and_a_detail_list(self):
        report = hook._render_report(
            dml_path=Path("x.sql"),
            result=self._quality(
                ok=False,
                warnings=["字段 'pay_cnt_udid' 非空率 0.4000 < 阈值 0.80"],
            ),
        )

        assert "| 字段名 |" in report
        assert "⚠️ 告警" in report
        assert "告警明细" in report
        assert "pay_cnt_udid" in report

    def test_table_is_absent_when_there_are_no_details(self):
        report = hook._render_report(
            dml_path=Path("x.sql"),
            result={"passed": True, "quality_check": {"ok": True, "checks": ["null_rate"]}},
        )

        assert "全部通过" in report
        assert "| 字段名 |" not in report


# ============================================================================
# Frontend delivery
# ============================================================================
class TestFrontendDelivery:
    def test_report_is_merged_into_the_last_ai_message(self, tmp_path):
        """非流式链路（chat / REST / A2A non-stream）靠 messages[-1] 取最终回复。

        所以报告必须**并入**最后一条 AI 消息 —— 追加成新消息会顶掉 agent 自己
        的交付说明（REST 的 _format_result 只取 messages[-1]）。
        """
        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)

        with patch.object(
            hook,
            "_invoke_trial_compute",
            return_value={
                "passed": True,
                "quality_check": {
                    "ok": True,
                    "checks": ["null_rate"],
                    "sample_size": 10000,
                    "warnings": [],
                    "details": {"col1": {"null_rate": 1.0, "single_value_ratio": 0.1}},
                },
            },
        ):
            result = hook.dataops_quality_check_post(_state(), runtime)

        assert len(result["messages"]) == 1
        content = result["messages"][0].content
        assert "| 字段名 |" in content

    def test_report_does_not_replace_the_agent_delivery_summary(self, tmp_path):
        """agent 自己的交付说明必须保留 —— 不能被报告顶掉。"""
        from langchain_core.messages import AIMessage

        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)
        state = {
            "complete": True,
            "run_id": 1,
            "messages": [AIMessage(content="已完成交付，SQL 见 sql/insert_x.sql。")],
        }

        with patch.object(
            hook,
            "_invoke_trial_compute",
            return_value={
                "passed": True,
                "quality_check": {
                    "ok": True,
                    "checks": ["null_rate"],
                    "sample_size": 10000,
                    "warnings": [],
                    "details": {"col1": {"null_rate": 1.0, "single_value_ratio": 0.1}},
                },
            },
        ):
            result = hook.dataops_quality_check_post(state, runtime)

        assert len(result["messages"]) == 1
        content = result["messages"][0].content
        # 交付说明 + 表格，都在最后一条消息里
        assert "已完成交付" in content
        assert "| 字段名 |" in content

    def test_report_is_emitted_to_the_stream(self, tmp_path):
        """流式链路里 state 变更不会再被 yield —— 必须额外推一条 output_msg。"""
        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)
        emitted: list[dict] = []

        with patch.object(
            hook,
            "_invoke_trial_compute",
            return_value={
                "passed": True,
                "quality_check": {
                    "ok": True,
                    "checks": ["null_rate"],
                    "sample_size": 10000,
                    "warnings": [],
                    "details": {"col1": {"null_rate": 1.0, "single_value_ratio": 0.1}},
                },
            },
        ), patch.object(hook, "get_stream_writer", return_value=emitted.append):
            hook.dataops_quality_check_post(_state(), runtime)

        assert len(emitted) == 1
        assert emitted[0]["type"] == "output_msg"
        assert "| 字段名 |" in emitted[0]["content"]

    def test_stream_emission_failure_does_not_break_the_hook(self, tmp_path):
        """流式推送失败不能影响交付 —— state 路径仍然要拿到报告。"""
        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)

        def _boom(_event):
            raise RuntimeError("stream closed")

        with patch.object(
            hook,
            "_invoke_trial_compute",
            return_value={
                "passed": True,
                "quality_check": {
                    "ok": True,
                    "checks": ["null_rate"],
                    "sample_size": 10000,
                    "warnings": [],
                    "details": {"col1": {"null_rate": 1.0, "single_value_ratio": 0.1}},
                },
            },
        ), patch.object(hook, "get_stream_writer", return_value=_boom):
            result = hook.dataops_quality_check_post(_state(), runtime)

        assert len(result["messages"]) == 1


# ============================================================================
# Node-level post-hook (HOOKS.nodes.planner.post)
# ============================================================================
class TestNodeLevelPostHook:
    """节点级 hook 收到的是**节点返回的增量**，不是完整图状态。

    ``BaseNode.aprocess`` 把 ``original_state``（节点输入的完整状态）作为
    keyword-only 参数传给 post-hook；hook 必须用它来拿 ``run_id`` 等字段。
    """

    def _quality(self):
        return {
            "passed": True,
            "quality_check": {
                "ok": True,
                "checks": ["null_rate"],
                "sample_size": 10000,
                "warnings": [],
                "details": {"col1": {"null_rate": 1.0, "single_value_ratio": 0.1}},
            },
        }

    def test_node_delta_is_merged_with_original_state(self, tmp_path):
        """节点增量里没有 run_id —— 必须从 original_state 取。"""
        from langchain_core.messages import AIMessage

        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)
        # 节点级：state 是节点返回的增量（没有 run_id）
        node_delta = {
            "messages": AIMessage(content="已完成交付。"),
            "complete": True,
            "num_turns": 1,
            "curr_iter": 3,
        }
        # original_state 是节点的输入状态（带 run_id）
        original_state = {"run_id": 42, "messages": [], "complete": False}

        with patch.object(hook, "_invoke_trial_compute", return_value=self._quality()):
            result = hook.dataops_quality_check_post(
                node_delta, runtime, original_state=original_state
            )

        # run_id 从 original_state 取到 → run guard 生效
        key, value = runtime.set_cache.call_args[0]
        assert key == hook._TRIAL_COMPUTE_DONE_KEY
        assert 42 in value
        # 报告进了 messages。
        # 注意形态：节点级增量的 messages 是**单条** AIMessage，hook 必须原样
        # 保持（BaseNode.aprocess 之后才会把它拼成列表并并入历史）。
        message = result["messages"]
        assert isinstance(message, AIMessage)
        assert "| 字段名 |" in message.content
        assert "已完成交付" in message.content

    def test_node_delta_return_does_not_duplicate_reducer_fields(self, tmp_path):
        """节点级 hook 的返回值是增量 —— 不能把 original_state 整体带回去。

        否则 run_id / complete 等 Annotated 字段会被 reducer 重复累加。
        """
        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)
        node_delta = {"messages": [], "complete": True, "num_turns": 1, "curr_iter": 3}
        original_state = {"run_id": 42, "messages": [], "complete": False, "num_turns": 7}

        with patch.object(hook, "_invoke_trial_compute", return_value=self._quality()):
            result = hook.dataops_quality_check_post(
                node_delta, runtime, original_state=original_state
            )

        # num_turns 保持节点增量的值（1），不是 original_state 的 7
        assert result["num_turns"] == 1
        # run_id 不在增量里（它不是节点返回的字段）
        assert "run_id" not in result

    def test_node_level_uses_original_state_when_delta_lacks_fields(self, tmp_path):
        """节点增量缺 complete 时，回落到 original_state 的判断。"""
        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)
        node_delta = {"messages": [], "complete": True}
        original_state = {"run_id": 9, "messages": [], "complete": False}

        with patch.object(hook, "_invoke_trial_compute", return_value=self._quality()):
            hook.dataops_quality_check_post(node_delta, runtime, original_state=original_state)

        key, value = runtime.set_cache.call_args[0]
        assert 9 in value


# ============================================================================
# Failure isolation
# ============================================================================
class TestFailureIsolation:
    def test_hook_never_raises(self, tmp_path):
        _deliverable(tmp_path)
        runtime = _runtime(tmp_path)

        with patch.object(hook, "_invoke_trial_compute", side_effect=RuntimeError("boom")):
            result = hook.dataops_quality_check_post(_state(), runtime)

        # State is returned unchanged instead of propagating the error.
        assert result["messages"] == []
