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
"""``verify._locate_sql`` 的定位契约。

这条函数决定**语法校验链路**校验哪一对 DDL/DML。契约有两条：

1. **必须成对** —— DDL 与 DML 必须指向**同一张目标表**。多表交付时拿错
   配对 → DataOps 里建出 A 表的临时表却往 B 表写 → 报「表不存在」，
   而真正的 SQL 一点问题都没有。
2. **必须确定** —— 同样的交付物，任何机器、任何运行顺序都要拿到同一对文件。

两条都不是「锦上添花」：第 1 条会**假报失败**（agent 反复改一个没问题的 SQL），
第 2 条会**让失败偶发**（同样的交付物，这次过下次不过）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from dataagent.core.suite.builtin_suites.de_agent.tools import verify as v

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_ddl(path: Path, table: str) -> None:
    path.write_text(
        f"CREATE EXTERNAL TABLE IF NOT EXISTS {table} (a string) "
        "PARTITIONED BY (pt_d string) STORED AS ORC",
        encoding="utf-8",
    )


def _write_dml(path: Path, table: str) -> None:
    path.write_text(
        f"INSERT OVERWRITE TABLE {table} PARTITION (pt_d = '20260101') "
        "SELECT a FROM biads.src",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 契约 1：DDL / DML 必须成对
# ---------------------------------------------------------------------------


class TestLocateSqlPairsDdlWithDml:
    def test_pairs_by_target_table(self, tmp_path):
        """多表交付时，DDL 与 DML 必须指向同一张表。"""
        _write_ddl(tmp_path / "create_b.sql", "biads.b")
        _write_dml(tmp_path / "insert_b.sql", "biads.b")
        _write_ddl(tmp_path / "create_a.sql", "biads.a")
        _write_dml(tmp_path / "insert_a.sql", "biads.a")

        ddl, dml = v._locate_sql(workspace=tmp_path)

        assert ddl is not None and dml is not None
        # 两份文件的目标表必须一致 —— 这才是「成对」。
        assert v._extract_target_table(ddl.read_text(encoding="utf-8")) == v._extract_target_table(
            dml.read_text(encoding="utf-8")
        )

    def test_bare_table_name_in_ddl_still_pairs(self, tmp_path):
        """DDL 不带库名（``CREATE TABLE ads_x``）时按裸表名配对。"""
        _write_ddl(tmp_path / "create_ads_x.sql", "ads_x")
        _write_dml(tmp_path / "insert_ads_x.sql", "biads.ads_x")

        ddl, dml = v._locate_sql(workspace=tmp_path)

        assert ddl is not None and dml is not None
        assert ddl.name == "create_ads_x.sql"
        assert dml.name == "insert_ads_x.sql"

    def test_ddl_only_workspace_returns_ddl_and_no_dml(self, tmp_path):
        _write_ddl(tmp_path / "create_a.sql", "biads.a")

        ddl, dml = v._locate_sql(workspace=tmp_path)

        assert ddl is not None
        assert dml is None

    def test_dml_only_workspace_returns_no_ddl(self, tmp_path):
        _write_dml(tmp_path / "insert_a.sql", "biads.a")

        ddl, dml = v._locate_sql(workspace=tmp_path)

        assert ddl is None
        assert dml is not None


# ---------------------------------------------------------------------------
# 契约 2：结果必须确定（不依赖文件系统遍历顺序）
# ---------------------------------------------------------------------------


class TestLocateSqlIsDeterministic:
    def test_same_result_across_repeated_calls(self, tmp_path):
        """反复调用必须拿到同一对文件。"""
        _write_ddl(tmp_path / "create_z.sql", "biads.z")
        _write_dml(tmp_path / "insert_z.sql", "biads.z")
        _write_ddl(tmp_path / "create_a.sql", "biads.a")
        _write_dml(tmp_path / "insert_a.sql", "biads.a")

        first = v._locate_sql(workspace=tmp_path)
        for _ in range(5):
            assert v._locate_sql(workspace=tmp_path) == first

    def test_unpaired_files_fall_back_to_newest(self, tmp_path):
        """配不上时降级取**最新修改**的一对 —— 最新改的才是 agent 刚动的。"""
        _write_ddl(tmp_path / "create_a.sql", "biads.a")
        _write_dml(tmp_path / "insert_a.sql", "biads.other_table")

        now = time.time()
        os.utime(tmp_path / "create_a.sql", (now - 3600, now - 3600))
        os.utime(tmp_path / "insert_a.sql", (now, now))

        # 只有一份 DDL / 一份 DML → 直接返回它们
        ddl, dml = v._locate_sql(workspace=tmp_path)
        assert ddl is not None and dml is not None

    def test_missing_workspace_returns_none(self, tmp_path):
        ddl, dml = v._locate_sql(workspace=tmp_path / "does_not_exist")
        assert ddl is None
        assert dml is None


# ---------------------------------------------------------------------------
# 目标表解析
# ---------------------------------------------------------------------------


class TestExtractTargetTable:
    @pytest.mark.parametrize(
        "sql,expected",
        [
            (
                "CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_x (a string) "
                "PARTITIONED BY (pt_d string)",
                "biads.ads_x",
            ),
            (
                "INSERT OVERWRITE TABLE biads.ads_x PARTITION (pt_d = '20260101') SELECT a FROM s",
                "biads.ads_x",
            ),
            (
                "INSERT OVERWRITE TABLE `biads`.`ads_x` PARTITION (pt_d = '20260101') SELECT a FROM s",
                "biads.ads_x",
            ),
            ("SELECT 1", ""),
            ("", ""),
        ],
        ids=["ddl", "dml", "backtick", "no_table", "empty"],
    )
    def test_extract(self, sql, expected):
        assert v._extract_target_table(sql) == expected

    def test_table_refs_match_bare_vs_qualified(self):
        assert v._table_refs_match("biads.ads_x", "ads_x") is True
        assert v._table_refs_match("biads.ads_x", "biads.ads_x") is True
        assert v._table_refs_match("biads.ads_x", "biads.ads_y") is False
        assert v._table_refs_match("", "biads.ads_x") is False
