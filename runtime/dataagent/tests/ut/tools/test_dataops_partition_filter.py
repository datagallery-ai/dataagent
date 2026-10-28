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
"""Unit tests for partition handling in the dataops probe / validate tools.

The contract under test: **all dataops read paths use the day partition
``pt_d`` only**. Hour / minute partitions (``pt_h`` / ``pt_i``) are never
used as the partition filter, because:

- the probe tool's whitelist only accepts ``pt_d = 'YYYYMMDD'``;
- the quality-check / direct-validation paths must agree with the probe path
  (otherwise "probe says the table is fine" and "quality check says the
  column is empty" would disagree for no visible reason).
"""

from __future__ import annotations

import pytest

from dataagent.actions.tools.local_tool.dataops_probe_tool import (
    _PARTITION_FILTER_RE,
    _build_probe_sql,
    _default_partition_filter,
)
from dataagent.actions.tools.local_tool.dataops_validate_tool import (
    extract_insert_partition_filter,
)


# ============================================================================
# extract_insert_partition_filter —— 归一化成 pt_d
# ============================================================================
class TestExtractInsertPartitionFilter:
    def test_pt_d_is_used_as_is(self):
        sql = "INSERT OVERWRITE TABLE biads.t SELECT a FROM s WHERE pt_d = '20261020'"
        assert extract_insert_partition_filter(sql) == "pt_d = '20261020'"

    def test_hour_partition_is_normalized_to_pt_d(self):
        """小时分区不能直接用作过滤条件 —— 归一化成同一天的 pt_d。"""
        sql = "INSERT OVERWRITE TABLE biads.t SELECT a FROM s WHERE pt_h = '2026101214'"
        assert extract_insert_partition_filter(sql) == "pt_d = '20261012'"

    def test_minute_partition_is_normalized_to_pt_d(self):
        sql = (
            "INSERT OVERWRITE TABLE biads.t SELECT a FROM s "
            "WHERE pt_i = '202610121430'"
        )
        assert extract_insert_partition_filter(sql) == "pt_d = '20261012'"

    def test_pt_d_wins_over_other_partition_columns(self):
        """同时有 pt_d 和 pt_h 时优先用 pt_d（天分区口径）。"""
        sql = (
            "INSERT OVERWRITE TABLE biads.t SELECT a FROM s "
            "WHERE pt_d = '20261020' AND pt_h = '2026102014'"
        )
        assert extract_insert_partition_filter(sql) == "pt_d = '20261020'"

    def test_composite_hour_and_minute_uses_the_day(self):
        sql = (
            "INSERT OVERWRITE TABLE biads.t SELECT a FROM s "
            "WHERE pt_d = '20261020' AND pt_h = '2026102014' AND pt_i = '202610201430'"
        )
        assert extract_insert_partition_filter(sql) == "pt_d = '20261020'"

    def test_placeholder_is_not_a_literal_date(self):
        sql = "INSERT OVERWRITE TABLE biads.t SELECT a FROM s WHERE pt_d = '$date'"
        assert extract_insert_partition_filter(sql) == ""

    def test_non_partition_column_is_ignored(self):
        """业务字段的等值条件不能被误抓成分区。"""
        sql = "INSERT OVERWRITE TABLE biads.t SELECT a FROM s WHERE user_id = '20261020'"
        assert extract_insert_partition_filter(sql) == ""

    def test_no_partition_returns_empty(self):
        sql = "INSERT OVERWRITE TABLE biads.t SELECT a FROM s"
        assert extract_insert_partition_filter(sql) == ""

    def test_empty_sql(self):
        assert extract_insert_partition_filter("") == ""

    def test_comment_is_stripped(self):
        sql = (
            "INSERT OVERWRITE TABLE biads.t SELECT a FROM s "
            "/* pt_d = '20260101' */ WHERE pt_d = '20261020'"
        )
        assert extract_insert_partition_filter(sql) == "pt_d = '20261020'"


# ============================================================================
# probe tool whitelist —— 只认 pt_d
# ============================================================================
class TestProbePartitionWhitelist:
    @pytest.mark.parametrize(
        "raw",
        [
            "pt_d = '20261012'",
            "pt_d='20261012'",
            "  pt_d = '20261012'  ",
        ],
    )
    def test_pt_d_forms_are_accepted(self, raw):
        assert _PARTITION_FILTER_RE.match(raw) is not None

    @pytest.mark.parametrize(
        "raw",
        [
            "pt_d = '20261012' AND pt_h = '2026101214'",
            "pt_d = '20261012' AND pt_h = '2026101214' AND pt_i = '202610121430'",
            "pt_h = '2026101214'",
            "pt_i = '202610121430'",
            "pt = '20261012'",
            "dt = '20261012'",
            "p_date = '20261012'",
        ],
    )
    def test_non_pt_d_forms_are_rejected(self, raw):
        """小时/分钟分区及其它列名一律拒绝。"""
        assert _PARTITION_FILTER_RE.match(raw) is None


# ============================================================================
# 默认值 + SQL 拼装
# ============================================================================
class TestProbeDefaultFilter:
    def test_default_filter_is_pt_d_only(self):
        assert _default_partition_filter("dws.user_action_dm").startswith("pt_d = '")
        assert _default_partition_filter("dws.user_action_hm").startswith("pt_d = '")
        assert _default_partition_filter("dws.user_action_im").startswith("pt_d = '")

    def test_default_filter_has_no_hour_or_minute(self):
        raw = _default_partition_filter("dws.user_action_im")
        assert "pt_h" not in raw
        assert "pt_i" not in raw

    def test_default_filter_matches_the_whitelist(self):
        assert _PARTITION_FILTER_RE.match(_default_partition_filter("x.y")) is not None

    def test_probe_sql_uses_the_filter_verbatim(self):
        sql = _build_probe_sql("dws.t", ["a", "b"], 5, "pt_d = '20261012'")
        assert sql == "SELECT `a`, `b` FROM `dws`.`t` WHERE pt_d = '20261012' LIMIT 5"
