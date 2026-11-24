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
"""「数据示例」的抽样捕获单测。

**为什么需要抽样**：确定性 checker（空值率 / 单一值比率 / 异常值占比）
只返回**聚合值** —— 报告里只有「非空率 0.9500」，用户看不到一行业务数据。
没有数据示例，「校验通过」就只是没有内容支撑的结论。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dataagent.extensions.loading import PythonLoader

_LOADER = PythonLoader()
_PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


def _module(name):
    _LOADER.load(_PLUGIN, f"utils/{name}.py:__name__")
    return _LOADER.modules[(_PLUGIN / "utils" / f"{name}.py").resolve()]


flow = _module("dataops_correctness_flow")
qc = _module("dataops_quality_check")


class _Exec:
    """记录被执行的 SQL，返回固定行。"""

    def __init__(self, rows, status="completed"):
        self.rows = rows
        self.status = status
        self.sqls: list[str] = []

    async def __call__(self, sql):
        self.sqls.append(sql)
        return self.status, self.rows


# ============================================================================
# record_sample_rows
# ============================================================================
class TestRecordSampleRows:
    def test_rows_are_normalized(self):
        rows = flow.record_sample_rows([{"a": 1, "b": "x"}])

        assert rows == [{"row_index": 1, "cells": {"a": 1, "b": "x"}}]

    def test_long_values_are_truncated(self):
        """超长的值不能把报告撑爆 —— 但要标出截断。"""
        rows = flow.record_sample_rows([{"a": "x" * 500}])

        value = rows[0]["cells"]["a"]
        assert len(value) < 500
        assert value.endswith("…")

    def test_long_lists_are_trimmed_with_a_count(self):
        rows = flow.record_sample_rows([{"a": list(range(100))}])

        value = rows[0]["cells"]["a"]
        assert "共 100 项" in value

    def test_numbers_keep_their_type(self):
        """数字转成字符串会丢掉精度语义 —— 必须原样保留。"""
        rows = flow.record_sample_rows([{"a": 3, "b": 0.5, "c": True}])

        assert rows[0]["cells"]["a"] == 3
        assert rows[0]["cells"]["b"] == 0.5
        assert rows[0]["cells"]["c"] is True

    def test_none_is_preserved(self):
        rows = flow.record_sample_rows([{"a": None}])

        assert rows[0]["cells"]["a"] is None

    def test_empty_input_returns_empty(self):
        assert flow.record_sample_rows([]) == []
        assert flow.record_sample_rows(None) == []

    def test_limit_is_respected(self):
        rows = flow.record_sample_rows([{"a": i} for i in range(10)], limit=3)

        assert len(rows) == 3


# ============================================================================
# _capture_sample_rows
# ============================================================================
class TestCaptureSampleRows:
    def test_captures_the_sampled_rows(self):
        executor = _Exec([{"udid": "u1", "cnt": 3}])

        rows = asyncio.run(
            qc._capture_sample_rows(
                execute=executor,
                target_table="biads.t",
                fields=["udid", "cnt"],
                primary_key_columns=("udid",),
                partition_filter="",
                limit=5,
            )
        )

        assert rows == [{"row_index": 1, "cells": {"udid": "u1", "cnt": 3}}]

    def test_primary_key_columns_come_first(self):
        """主键排在前面 —— 用户要能一眼定位到记录。"""
        executor = _Exec([{"udid": "u1", "cnt": 3}])

        asyncio.run(
            qc._capture_sample_rows(
                execute=executor,
                target_table="biads.t",
                fields=["cnt", "udid"],
                primary_key_columns=("udid",),
                partition_filter="",
                limit=5,
            )
        )

        sql = executor.sqls[0]
        assert sql.index("`udid`") < sql.index("`cnt`")

    def test_partition_filter_is_applied(self):
        """抽样必须与其它校验项**同一分区口径** —— 否则抽到的不是同一批数据。"""
        executor = _Exec([{"udid": "u1"}])

        asyncio.run(
            qc._capture_sample_rows(
                execute=executor,
                target_table="biads.t",
                fields=["udid"],
                primary_key_columns=(),
                partition_filter="pt_d = '20261020'",
                limit=5,
            )
        )

        assert "pt_d = '20261020'" in executor.sqls[0]

    def test_executor_failure_returns_empty_without_raising(self):
        """抽样失败**绝不能让整轮校验崩掉** —— 它只是报告的锦上添花。"""

        async def _boom(_sql):
            raise RuntimeError("dataops down")

        rows = asyncio.run(
            qc._capture_sample_rows(
                execute=_boom,
                target_table="biads.t",
                fields=["udid"],
                primary_key_columns=(),
                partition_filter="",
                limit=5,
            )
        )

        assert rows == []

    def test_non_completed_status_returns_empty(self):
        executor = _Exec(None, status="timeout")

        rows = asyncio.run(
            qc._capture_sample_rows(
                execute=executor,
                target_table="biads.t",
                fields=["udid"],
                primary_key_columns=(),
                partition_filter="",
                limit=5,
            )
        )

        assert rows == []

    def test_no_fields_returns_empty(self):
        executor = _Exec([{"a": 1}])

        rows = asyncio.run(
            qc._capture_sample_rows(
                execute=executor,
                target_table="biads.t",
                fields=[],
                primary_key_columns=(),
                partition_filter="",
                limit=5,
            )
        )

        assert rows == []
        assert not executor.sqls
