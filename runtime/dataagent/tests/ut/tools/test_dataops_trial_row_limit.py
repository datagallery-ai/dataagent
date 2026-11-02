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
"""Unit tests for the trial-compute row limit (``_limit_insert_rows``).

Why this exists: the delivery post-hook's quality check only reads the first
``quality_check.sample_size`` rows (default 10000) of the produced data, so
writing more rows into the scratch table costs real time and buys nothing.
The trial INSERT is therefore capped at the sample size.

The contracts under test:

1. The rewrite is **purely additive** — a single ``LIMIT N`` is appended, the
   rest of the SQL is byte-identical (platform UDFs, comments, ``LATERAL
   VIEW`` … must survive untouched).
2. The rewrite is **conservative** — anything it cannot reason about
   confidently (not an INSERT, ``INSERT ... VALUES``, an existing top-level
   ``LIMIT``) is left alone.
3. It is **opt-in** — the static validation chain must not be truncated.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.local_tool import dataops_validate_tool as vt


# ============================================================================
# _clamp_row_limit
# ============================================================================
class TestClampRowLimit:
    def test_default_for_non_numeric(self):
        assert vt._clamp_row_limit("abc") == vt._DEFAULT_TRIAL_ROW_LIMIT
        assert vt._clamp_row_limit(None) == vt._DEFAULT_TRIAL_ROW_LIMIT

    def test_non_positive_falls_back_to_default(self):
        """0 / 负数不能让试算退化成全量写入。"""
        assert vt._clamp_row_limit(0) == vt._DEFAULT_TRIAL_ROW_LIMIT
        assert vt._clamp_row_limit(-5) == vt._DEFAULT_TRIAL_ROW_LIMIT

    def test_hard_cap(self):
        assert vt._clamp_row_limit(10**9) == vt._MAX_TRIAL_ROW_LIMIT

    def test_normal_value_passes_through(self):
        assert vt._clamp_row_limit(500) == 500


# ============================================================================
# _limit_insert_rows — the rewrite itself
# ============================================================================
class TestLimitInsertRows:
    def test_appends_limit_to_insert(self):
        sql = (
            "INSERT OVERWRITE TABLE biads.ads_x PARTITION (pt_d = '$date')\n"
            "SELECT udid, cnt FROM dws.a WHERE pt_d = '$date'\n"
        )
        out = vt._limit_insert_rows(sql, 10000)
        assert out.endswith("WHERE pt_d = '$date' LIMIT 10000\n")

    def test_limit_goes_before_trailing_semicolon(self):
        sql = "INSERT OVERWRITE TABLE biads.x SELECT a FROM t;"
        out = vt._limit_insert_rows(sql, 100)
        assert out == "INSERT OVERWRITE TABLE biads.x SELECT a FROM t LIMIT 100;"

    def test_limit_goes_before_trailing_comment(self):
        sql = "INSERT OVERWRITE TABLE biads.x SELECT a FROM t\n-- trailing note\n"
        out = vt._limit_insert_rows(sql, 100)
        assert out == "INSERT OVERWRITE TABLE biads.x SELECT a FROM t LIMIT 100\n-- trailing note\n"

    def test_platform_udf_is_preserved_verbatim(self):
        """``!bicoredata.IsEmpty`` 不能被改写（AST 往返会把它变成 NOT ...）。"""
        sql = (
            "INSERT OVERWRITE TABLE biads.x SELECT a FROM t "
            "WHERE !bicoredata.IsEmpty(a) AND pt_d = '$date'"
        )
        out = vt._limit_insert_rows(sql, 100)
        assert "!bicoredata.IsEmpty(a)" in out
        assert "NOT bicoredata" not in out
        assert out.endswith("LIMIT 100")

    def test_lateral_view_is_preserved(self):
        sql = (
            "INSERT OVERWRITE TABLE biads.x SELECT udid, col FROM dws.src "
            "LATERAL VIEW POSEXPLODE(arr) e AS pos, col"
        )
        out = vt._limit_insert_rows(sql, 100)
        assert "LATERAL VIEW POSEXPLODE(arr) e AS pos, col" in out
        assert out.endswith("LIMIT 100")

    def test_union_all_limit_applies_to_whole_query(self):
        sql = (
            "INSERT OVERWRITE TABLE biads.x SELECT a FROM t1 WHERE pt_d = '$date'\n"
            "UNION ALL\n"
            "SELECT a FROM t2 WHERE pt_d = '$date'\n"
        )
        out = vt._limit_insert_rows(sql, 100)
        # LIMIT 落在整个 queryTerm（UNION 结果）之后
        assert out.rstrip().endswith("LIMIT 100")
        assert out.count("LIMIT 100") == 1

    def test_cte_insert_is_limited(self):
        sql = (
            "WITH cte AS (SELECT a FROM t1 WHERE pt_d = '$date')\n"
            "INSERT OVERWRITE TABLE biads.x SELECT a FROM cte\n"
        )
        out = vt._limit_insert_rows(sql, 100)
        assert out.rstrip().endswith("LIMIT 100")

    def test_inner_limit_does_not_block_outer_limit(self):
        """子查询里的 LIMIT 不算「顶层已有 LIMIT」—— 外层仍然要截断。"""
        sql = "INSERT OVERWRITE TABLE biads.x SELECT a FROM (SELECT a FROM t LIMIT 10) s"
        out = vt._limit_insert_rows(sql, 100)
        assert out.endswith("LIMIT 100")
        assert "LIMIT 10" in out

    def test_existing_top_level_limit_is_preserved(self):
        """交付物自己写了 LIMIT → 不叠加，保留它的上限。"""
        sql = "INSERT OVERWRITE TABLE biads.x SELECT a FROM t LIMIT 500"
        assert vt._limit_insert_rows(sql, 100) == sql

    def test_non_insert_is_untouched(self):
        sql = "CREATE TABLE biads.t (a string)"
        assert vt._limit_insert_rows(sql, 100) == sql

    def test_insert_values_is_untouched(self):
        """``INSERT ... VALUES`` 加 LIMIT 会破坏语法。"""
        sql = "INSERT OVERWRITE TABLE biads.x PARTITION (pt_d = '$date') VALUES (1, 2)"
        assert vt._limit_insert_rows(sql, 100) == sql

    def test_select_is_untouched(self):
        sql = "SELECT a FROM t WHERE pt_d = '$date'"
        assert vt._limit_insert_rows(sql, 100) == sql

    def test_column_named_limit_is_not_a_keyword(self):
        """``t.limit`` 是列名，不是 LIMIT 子句。"""
        sql = "INSERT OVERWRITE TABLE biads.x SELECT t.limit FROM dws.t t"
        out = vt._limit_insert_rows(sql, 100)
        assert out.endswith("LIMIT 100")

    def test_limit_is_not_treated_as_function_call(self):
        """``limit(...)`` 是函数调用，不是 LIMIT 子句。"""
        sql = "INSERT OVERWRITE TABLE biads.x SELECT limit(a) FROM t"
        out = vt._limit_insert_rows(sql, 100)
        assert out.endswith("LIMIT 100")

    def test_zero_limit_is_a_no_op(self):
        sql = "INSERT OVERWRITE TABLE biads.x SELECT a FROM t"
        assert vt._limit_insert_rows(sql, 0) == sql

    def test_empty_sql_is_a_no_op(self):
        assert vt._limit_insert_rows("", 100) == ""
        assert vt._limit_insert_rows("   ", 100) == "   "

    def test_rest_of_sql_is_byte_identical(self):
        """除了插入的 LIMIT，其余部分必须逐字节保持原样。"""
        sql = (
            "INSERT OVERWRITE TABLE biads.ads_x PARTITION (pt_d = '$date')\n"
            "SELECT\n"
            "  udid_sha256,\n"
            "  bicoredata.SHA256(udid) AS udid_sha256\n"
            "FROM dws.src_user_dm\n"
            "WHERE pt_d = '$date'\n"
            "  AND !bicoredata.IsEmpty(udid)\n"
        )
        out = vt._limit_insert_rows(sql, 10000)
        assert out.replace(" LIMIT 10000", "") == sql


# ============================================================================
# Wiring — the row limit is opt-in and off on the static validation chain
# ============================================================================
def _make_resource(*, timeout_s: int = 5, poll_interval_ms: int = 50):
    from dataagent.resources.catalog.models import Resource

    return Resource(
        id="dataops",
        name="dataops",
        category="executable",
        metadata={"enabled": True, "timeout_s": timeout_s, "poll_interval_ms": poll_interval_ms},
        transport={"type": "mcp", "url": "http://localhost:8767/mcp"},
    )


def _wired_coordinator(*, resource, submit, poll, collect):
    coordinator = MagicMock()
    coordinator.catalog = SimpleNamespace(get=lambda key: resource if key == "dataops" else None)
    coordinator.submit_job.return_value = submit
    coordinator.poll.return_value = poll
    coordinator.collect.return_value = collect
    return coordinator


def _ctx_with(resource, coordinator):
    runtime = MagicMock()
    runtime.ensure_resource_coordinator.return_value = coordinator
    runtime.user_id = "u1"
    return ToolExecutionContext(runtime=runtime)  # type: ignore[arg-type]


class TestRowLimitWiring:
    def _run(self, **kwargs):
        resource = _make_resource()
        coordinator = _wired_coordinator(
            resource=resource,
            submit={"status": "queued", "job_id": "j-1"},
            poll={"status": "completed"},
            collect={"status": "completed"},
        )
        ctx = _ctx_with(resource, coordinator)
        sql = "INSERT OVERWRITE TABLE biads.x SELECT a FROM t WHERE pt_d = '$date'"
        asyncio.run(vt.dataops_validate_sql(sql, _tool_context=ctx, **kwargs))
        return coordinator.submit_job.call_args.kwargs["command"]

    def test_row_limit_is_off_by_default(self):
        """语法校验链路不改写 SQL —— 它要验证 SQL 的真实行为。"""
        submitted = self._run()
        assert "LIMIT" not in submitted

    def test_row_limit_applies_when_enabled(self):
        submitted = self._run(_row_limit=10000)
        assert submitted.rstrip().endswith("LIMIT 10000")

    def test_row_limit_zero_is_a_no_op(self):
        submitted = self._run(_row_limit=0)
        assert "LIMIT" not in submitted
