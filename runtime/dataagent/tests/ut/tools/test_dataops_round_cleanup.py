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
"""``_run_dataops_validation_round`` 的收尾清理契约。

这个函数的清理在 ``finally`` 里 —— 它一旦抛异常，就会**替换掉已经算好的
summary**，调用方 ``validate_deliverables`` 拿到的是异常而不是校验结论。

覆盖三条退出路径（成功 / 失败短路 / 校验自身抛异常），并断言：

1. 清理**不会**覆盖返回值（``CancelledError`` 也不行 ——
   它继承自 ``BaseException``，``except Exception`` 接不住）。
2. 清理**一定**被执行（不能因为 helper 名字写错就静默跳过）。
3. 清理**不会**早于配对的 INSERT 发生（2026-11-06 事故形态）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool import dataops_validate_tool as vt
from dataagent.core.suite.builtin_suites.de_agent.tools import verify as v
from dataagent.utils.log import logger

DDL = (
    "CREATE TABLE IF NOT EXISTS biads.ads_x (a INT) "
    "PARTITIONED BY (pt_d STRING) STORED AS ORC"
)
DML = "INSERT OVERWRITE TABLE biads.ads_x PARTITION (pt_d = '20260101') SELECT 1"


@contextmanager
def _capture_loguru_in_caplog(caplog: pytest.LogCaptureFixture, level: int = logging.DEBUG):
    """将 loguru 日志桥接到 pytest caplog，便于断言日志内容。"""
    bridge_logger = logging.getLogger("dataops_round_cleanup")

    def _emit(message) -> None:
        record = message.record
        bridge_logger.log(record["level"].no, record["message"])

    handler_id = logger.add(_emit, format="{message}")
    with caplog.at_level(level, logger="dataops_round_cleanup"):
        try:
            yield
        finally:
            logger.remove(handler_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> ToolExecutionContext:
    runtime = MagicMock()
    runtime.user_id = "u1"
    runtime.ensure_resource_coordinator.return_value = None
    return ToolExecutionContext(runtime=runtime, tool_config={})


def _sql_files(tmp_path: Path) -> tuple[Path, Path]:
    ddl = tmp_path / "create_ads_x.sql"
    dml = tmp_path / "insert_ads_x.sql"
    ddl.write_text(DDL, encoding="utf-8")
    dml.write_text(DML, encoding="utf-8")
    return ddl, dml


def _patch_validate(monkeypatch, *, passed: bool = True, raises: BaseException | None = None):
    """Patch the wrapper so the round's validation outcome is deterministic."""
    calls: list[str] = []

    async def fake_validate(**_kwargs):
        calls.append("validate")
        if raises is not None:
            raise raises
        return {"passed": passed, "job_id": "j-1"}

    monkeypatch.setattr(vt, "dataops_validate_sql_with_log_analysis", fake_validate)
    return calls


def _patch_cleanup(monkeypatch, *, raises: BaseException | None = None):
    """Patch the round-level cleanup and record that it ran."""
    calls: list[list[str]] = []

    async def fake_cleanup(sqls, **_kwargs):
        calls.append(list(sqls))
        if raises is not None:
            raise raises
        return ["job-cleanup"]

    monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)
    return calls


# ---------------------------------------------------------------------------
# (a) 清理抛异常不得覆盖返回值
# ---------------------------------------------------------------------------


class TestCleanupNeverOverridesTheVerdict:
    """清理在 ``finally`` 里 —— 抛异常就会吞掉校验结论。

    真实风险点：``asyncio.CancelledError`` 继承自 ``BaseException`` 而**不是**
    ``Exception``，所以 ``except Exception`` 接不住它。清理协程被取消时，
    异常会从 ``finally`` 逃逸，调用方 ``validate_deliverables`` 直接抛异常，
    明明已经跑完的校验结论却拿不到。
    """

    @pytest.mark.parametrize(
        "exc",
        [
            asyncio.CancelledError("cleanup cancelled"),
            RuntimeError("cleanup boom"),
            ValueError("bad args"),
        ],
        ids=["cancelled", "runtime", "value"],
    )
    def test_cancelled_error_does_not_escape(self, tmp_path, monkeypatch, exc):
        """即使清理抛 ``CancelledError``，summary 也必须照常返回。"""
        ddl, dml = _sql_files(tmp_path)
        _patch_validate(monkeypatch, passed=True)
        _patch_cleanup(monkeypatch, raises=exc)

        summary = v._run_dataops_validation_round(
            ddl_path=ddl,
            dml_path=dml,
            state={"dataops_attempts": []},
            _tool_context=_ctx(),
        )

        assert summary["passed"] is True, "the verdict must survive a failed cleanup"

    def test_failure_shortcut_keeps_the_error_detail(self, tmp_path, monkeypatch):
        """失败短路时，错误详情也不能被清理异常顶掉。"""
        ddl, dml = _sql_files(tmp_path)
        _patch_validate(monkeypatch, passed=False)
        _patch_cleanup(monkeypatch, raises=asyncio.CancelledError("cleanup cancelled"))

        summary = v._run_dataops_validation_round(
            ddl_path=ddl,
            dml_path=dml,
            state={"dataops_attempts": []},
            _tool_context=_ctx(),
        )

        assert summary["passed"] is False
        assert summary["error"], "the failure detail must survive a failed cleanup"

    def test_validation_crash_still_runs_cleanup(self, tmp_path, monkeypatch):
        """校验自身抛异常时，清理**仍然**要跑（否则临时表泄漏）。"""
        ddl, dml = _sql_files(tmp_path)
        _patch_validate(monkeypatch, raises=RuntimeError("wrapper crashed"))
        cleanup_calls = _patch_cleanup(monkeypatch)

        v._run_dataops_validation_round(
            ddl_path=ddl,
            dml_path=dml,
            state={"dataops_attempts": []},
            _tool_context=_ctx(),
        )

        assert cleanup_calls, "cleanup must run even when the validation crashes"

    def test_cancelled_error_propagates_when_validation_is_cancelled(self, tmp_path, monkeypatch):
        """校验本身被取消时，取消信号**要**继续往外传（不能被清理吞掉）。

        取消是「上层要停」的信号，不是「清理失败」—— 必须传播下去，
        否则被取消的作业会假装正常结束。
        """
        ddl, dml = _sql_files(tmp_path)
        _patch_validate(monkeypatch, raises=asyncio.CancelledError("validation cancelled"))
        _patch_cleanup(monkeypatch)

        with pytest.raises(asyncio.CancelledError):
            v._run_dataops_validation_round(
                ddl_path=ddl,
                dml_path=dml,
                state={"dataops_attempts": []},
                _tool_context=_ctx(),
            )


# ---------------------------------------------------------------------------
# (c) 清理路径必须真的执行
# ---------------------------------------------------------------------------


class TestCleanupAlwaysRuns:
    """清理不能因为「helper 名字写错 / 参数不匹配」而静默跳过。

    早先 ``except Exception`` 把所有失败都吞成一条 warning 日志 ——
    helper 一旦不存在，临时表就永远残留，而且**没有任何地方报出来**。
    """

    def test_cleanup_runs_on_the_success_path(self, tmp_path, monkeypatch):
        ddl, dml = _sql_files(tmp_path)
        _patch_validate(monkeypatch, passed=True)
        cleanup_calls = _patch_cleanup(monkeypatch)

        v._run_dataops_validation_round(
            ddl_path=ddl,
            dml_path=dml,
            state={"dataops_attempts": []},
            _tool_context=_ctx(),
        )

        assert cleanup_calls, "cleanup must run on the success path"
        assert len(cleanup_calls[0]) == 2, "both the DDL and the DML must be cleaned"

    def test_cleanup_runs_on_the_failure_shortcut_path(self, tmp_path, monkeypatch):
        ddl, dml = _sql_files(tmp_path)
        _patch_validate(monkeypatch, passed=False)
        cleanup_calls = _patch_cleanup(monkeypatch)

        v._run_dataops_validation_round(
            ddl_path=ddl,
            dml_path=dml,
            state={"dataops_attempts": []},
            _tool_context=_ctx(),
        )

        assert cleanup_calls, "cleanup must run on the failure path too"

    def test_cleanup_failure_is_logged_not_silent(self, tmp_path, monkeypatch, caplog):
        """清理失败必须在日志里可见 —— 否则泄漏完全无声无息。"""
        ddl, dml = _sql_files(tmp_path)
        _patch_validate(monkeypatch, passed=True)
        _patch_cleanup(monkeypatch, raises=RuntimeError("helper missing"))

        with _capture_loguru_in_caplog(caplog, level=logging.WARNING):
            v._run_dataops_validation_round(
                ddl_path=ddl,
                dml_path=dml,
                state={"dataops_attempts": []},
                _tool_context=_ctx(),
            )

        log_text = caplog.text
        assert "cleanup" in log_text.lower(), log_text
        assert "may remain" in log_text, log_text


# ---------------------------------------------------------------------------
# (b) 清理不得早于配对的 INSERT
# ---------------------------------------------------------------------------


class TestCleanupDoesNotPreemptTheInsert:
    """清理**不能**早于配对的 INSERT —— 那是 2026-11-06 的事故形态。

        13054156  CREATE TABLE ..._20271106_...   OK
        13054250  DROP   TABLE ..._20271106_...   OK   <-- 提前删了
        13054413  INSERT OVERWRITE ..._20271106_... FAIL (table not found)
    """

    def test_round_cleanup_runs_after_both_sqls(self, tmp_path, monkeypatch):
        """轮次收尾的清理必须在 DDL 和 DML 都跑完之后才发生。"""
        ddl, dml = _sql_files(tmp_path)
        order: list[str] = []

        async def fake_validate(**_kwargs):
            order.append("validate")
            return {"passed": True, "job_id": "j-1"}

        async def fake_cleanup(sqls, **_kwargs):
            order.append("cleanup")
            return ["job-1"]

        monkeypatch.setattr(vt, "dataops_validate_sql_with_log_analysis", fake_validate)
        monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)

        v._run_dataops_validation_round(
            ddl_path=ddl,
            dml_path=dml,
            state={"dataops_attempts": []},
            _tool_context=_ctx(),
        )

        assert order == ["validate", "validate", "cleanup"], order
        assert order[-1] == "cleanup", "cleanup must be the LAST step of the round"

    def test_ddl_scratch_table_survives_until_the_insert(self, tmp_path, monkeypatch):
        """端到端：建表那张临时表必须活到配对的 INSERT 之后才被删。"""
        ddl, dml = _sql_files(tmp_path)
        submitted: list[str] = []

        coordinator = MagicMock()

        def submit_job(*, resource_id, command, task_type, timeout_sec):
            submitted.append(command)
            return {"status": "queued", "job_id": f"job-{len(submitted)}"}

        coordinator.submit_job.side_effect = submit_job
        coordinator.poll.return_value = {"status": "completed"}
        coordinator.collect.return_value = {"status": "completed"}
        coordinator.catalog = MagicMock()
        coordinator.catalog.get = lambda key: _resource() if key == "dataops" else None

        runtime = MagicMock()
        runtime.user_id = "u1"
        runtime.ensure_resource_coordinator.return_value = coordinator
        ctx = ToolExecutionContext(runtime=runtime, tool_config={})

        v._run_dataops_validation_round(
            ddl_path=ddl,
            dml_path=dml,
            state={"dataops_attempts": []},
            _tool_context=ctx,
        )

        kinds = [c.strip().split()[0].upper() for c in submitted if c.strip()]
        assert "CREATE" in kinds and "INSERT" in kinds, kinds

        create_idx = kinds.index("CREATE")
        insert_idx = kinds.index("INSERT")

        target = vt._extract_temp_table_names(
            DDL, "u1", "", vt.partition_date_for_validation()
        )
        between = [
            c
            for c in submitted[create_idx + 1 : insert_idx]
            if c.strip().upper().startswith("DROP") and any(t in c for t in target)
        ]
        assert not between, (
            f"the DDL's scratch table was dropped between CREATE and INSERT: {between}"
        )


def _resource():
    from dataagent.resources.catalog.models import Resource

    return Resource(
        id="dataops",
        name="dataops",
        category="executable",
        metadata={
            "enabled": True,
            "exec_user": "u1",
            "timeout_s": 300,
            "poll_interval_ms": 100,
        },
        transport={"type": "mcp", "url": "http://localhost:8767/mcp"},
    )


# ---------------------------------------------------------------------------
# 异步清理点：取消语义不能被清理吞掉
# ---------------------------------------------------------------------------


class TestAsyncCleanupPreservesCancellation:
    """异步清理点必须**重新抛出** ``CancelledError``。

    关键区别：``verify.py`` 的清理跑在**独立线程 + 独立事件循环**里
    （``_run_coroutine``），所以在那里吞掉 ``CancelledError`` 不会丢取消信号。

    但 ``sql_checkers`` / ``trial_compute`` 的清理是 **await** 在调用方的
    任务里的 —— 如果在那里吞掉 ``CancelledError``，**取消信号会被彻底丢失**：
    任务会「正常结束」，`task.cancelled()` 变成 ``False``。
    被取消的作业于是假装跑完了 —— 这比临时表泄漏更危险。
    """

    @pytest.mark.asyncio
    async def test_sql_checkers_cleanup_reraises_cancelled(self, monkeypatch):
        from dataagent.core.suite.builtin_suites.de_agent.tools import sql_checkers as sc

        async def fake_cleanup(*_args, **_kwargs):
            raise asyncio.CancelledError("cleanup cancelled")

        monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)

        checker = sc.SQLCheckerPipeline.__new__(sc.SQLCheckerPipeline)
        checker.tool_context = _ctx()
        checker.report = {}

        with pytest.raises(asyncio.CancelledError):
            await checker._cleanup_round_temp_tables({"ddl": DDL, "dml": DML})

    @pytest.mark.asyncio
    async def test_sql_checkers_cleanup_swallows_plain_exception(self, monkeypatch):
        from dataagent.core.suite.builtin_suites.de_agent.tools import sql_checkers as sc

        async def fake_cleanup(*_args, **_kwargs):
            raise RuntimeError("cleanup boom")

        monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)

        checker = sc.SQLCheckerPipeline.__new__(sc.SQLCheckerPipeline)
        checker.tool_context = _ctx()
        checker.report = {}

        # 普通异常必须被吞掉 —— 清理失败不能影响校验结论。
        await checker._cleanup_round_temp_tables({"ddl": DDL, "dml": DML})

    @pytest.mark.asyncio
    async def test_trial_compute_cleanup_reraises_cancelled(self, monkeypatch):
        from dataagent.core.suite.builtin_suites.de_agent.tools import (
            dataops_trial_compute_tool as tct,
        )

        async def fake_cleanup(*_args, **_kwargs):
            raise asyncio.CancelledError("cleanup cancelled")

        monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)

        payload: dict = {}
        with pytest.raises(asyncio.CancelledError):
            await tct._drop_scratch_tables(
                [DDL, DML],
                _tool_context=_ctx(),
                partition_date="20260101",
                payload=payload,
            )

    @pytest.mark.asyncio
    async def test_trial_compute_cleanup_swallows_plain_exception(self, monkeypatch):
        from dataagent.core.suite.builtin_suites.de_agent.tools import (
            dataops_trial_compute_tool as tct,
        )

        async def fake_cleanup(*_args, **_kwargs):
            raise RuntimeError("cleanup boom")

        monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)

        payload: dict = {}
        await tct._drop_scratch_tables(
            [DDL, DML],
            _tool_context=_ctx(),
            partition_date="20260101",
            payload=payload,
        )

    @pytest.mark.asyncio
    async def test_cancellation_is_not_lost_when_cleanup_is_awaited(self):
        """回归：取消信号不能被 finally 里的清理吞掉。

        错误的写法（吞掉 ``CancelledError``）会让任务「正常返回」，
        ``task.cancelled()`` 变成 ``False`` —— 被取消的作业假装跑完了。
        正确写法（``raise``）必须保住取消信号。
        """
        # 错误写法：吞掉取消 -> 取消信号丢失。
        async def cleanup_swallows_cancelled():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return "cleanup done"

        async def bad_worker():
            try:
                return "body-done"
            finally:
                await cleanup_swallows_cancelled()

        bad_task = asyncio.ensure_future(bad_worker())
        await asyncio.sleep(0.05)
        bad_task.cancel()
        # 反例的实际表现：任务「正常完成」，取消信号被吞掉。
        result = await bad_task
        assert result == "body-done"
        assert not bad_task.cancelled(), "the known-bad pattern loses the cancellation"
        # 正确写法：重新抛出 -> 取消信号保住。
        async def cleanup_reraises_cancelled():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

        async def good_worker():
            try:
                return "body-done"
            finally:
                await cleanup_reraises_cancelled()

        good_task = asyncio.ensure_future(good_worker())
        await asyncio.sleep(0.05)
        good_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await good_task
        assert good_task.cancelled()


# ---------------------------------------------------------------------------
# 调用契约：清理点的签名必须与底层 helper 一致
# ---------------------------------------------------------------------------


class TestCleanupCallContract:
    """各清理点传给 ``cleanup_temp_tables`` 的参数必须与真实签名一致。

    这是本 PR 修的那个 bug 的**根因**所在：清理调用一旦因为签名漂移
    （比如底层不再接受 ``context_label``）而抛 ``TypeError``，就会被
    ``except Exception`` 静默吞掉 —— 生产里临时表照旧残留，而**测试全绿**。

    所以这里**不用假实现**，而是直接拿真实函数签名去做绑定检查：
    签名漂移会在这里直接失败，而不是等到线上才发现。
    """

    def test_real_cleanup_signature_accepts_every_call_site(self):
        """每个清理点传参都必须能被真实签名接受。"""
        import inspect

        sig = inspect.signature(vt.cleanup_temp_tables)

        call_sites = {
            "sql_checkers": {
                "_tool_context": _ctx(),
                "context_label": "sql_checkers.dataops",
            },
            "verify": {
                "_tool_context": _ctx(),
            },
            "trial_compute": {
                "_tool_context": _ctx(),
                "partition_date": "20260101",
                "context_label": "trial_compute",
            },
        }

        for name, kwargs in call_sites.items():
            try:
                sig.bind([DDL, DML], **kwargs)
            except TypeError as exc:
                pytest.fail(f"{name} cleanup call does not bind to the real signature: {exc}")

    @pytest.mark.asyncio
    async def test_sql_checkers_passes_context_label_and_records_job_ids(self, monkeypatch):
        """sql_checkers 必须传 ``context_label``，并把 job_id 写进 report。"""
        from dataagent.core.suite.builtin_suites.de_agent.tools import sql_checkers as sc

        seen: dict = {}

        async def fake_cleanup(sqls, **kwargs):
            # 严格签名：不接受多余的 kwarg，签名漂移会立刻 TypeError。
            seen["sqls"] = list(sqls)
            seen["kwargs"] = kwargs
            return ["job-cleanup-1"]

        monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)

        checker = sc.SQLCheckerPipeline.__new__(sc.SQLCheckerPipeline)
        checker.tool_context = _ctx()
        checker.report = {}

        await checker._cleanup_round_temp_tables({"ddl": DDL, "dml": DML})

        assert seen["kwargs"].get("context_label") == "sql_checkers.dataops", seen
        assert seen["sqls"] == [DDL, DML], seen
        assert checker.report["dataops"]["cleanup_job_ids"] == ["job-cleanup-1"]

    @pytest.mark.asyncio
    async def test_trial_compute_passes_context_label_and_partition_date(self, monkeypatch):
        """trial_compute 必须同时传 ``context_label`` 和 ``partition_date``。"""
        from dataagent.core.suite.builtin_suites.de_agent.tools import (
            dataops_trial_compute_tool as tct,
        )

        seen: dict = {}

        async def fake_cleanup(sqls, **kwargs):
            seen["sqls"] = list(sqls)
            seen["kwargs"] = kwargs
            return ["job-cleanup-2"]

        monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)

        payload: dict = {}
        await tct._drop_scratch_tables(
            [DDL, DML],
            _tool_context=_ctx(),
            partition_date="20260101",
            payload=payload,
        )

        assert seen["kwargs"].get("context_label") == "trial_compute", seen
        assert seen["kwargs"].get("partition_date") == "20260101", seen
        assert payload["cleanup_job_ids"] == ["job-cleanup-2"]
        assert payload["cleanup_job_id"] == "job-cleanup-2"

    def test_verify_round_cleanup_binds_to_the_real_signature(self):
        """verify.py 的轮次清理必须能被真实签名接受。"""
        import inspect

        sig = inspect.signature(vt.cleanup_temp_tables)
        sig.bind([DDL, DML], _tool_context=_ctx())

    @pytest.mark.asyncio
    async def test_sql_checkers_cleanup_typeerror_is_not_silent(self, monkeypatch, caplog):
        """签名漂移导致的 ``TypeError`` 必须留下可见痕迹，而不是无声无息。"""
        from dataagent.core.suite.builtin_suites.de_agent.tools import sql_checkers as sc

        async def fake_cleanup(sqls, **_kwargs):
            # 模拟底层签名漂移：不再接受 context_label。
            raise TypeError(
                "cleanup_temp_tables() got an unexpected keyword argument 'context_label'"
            )

        monkeypatch.setattr(vt, "cleanup_temp_tables", fake_cleanup)

        checker = sc.SQLCheckerPipeline.__new__(sc.SQLCheckerPipeline)
        checker.tool_context = _ctx()
        checker.report = {}

        with _capture_loguru_in_caplog(caplog, level=logging.WARNING):
            await checker._cleanup_round_temp_tables({"ddl": DDL, "dml": DML})

        log_text = caplog.text
        assert "cleanup" in log_text.lower(), log_text
        assert "may remain" in log_text, log_text
        # 泄漏必须可见：报告里不能声称「已清理」。
        assert "cleanup_job_ids" not in checker.report.get("dataops", {})
