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
"""Trial row-limit orchestration cases from main !561."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
LOADER = PythonLoader()
tct = sys.modules[LOADER.load(PLUGIN, "utils/dataops_trial_compute_tool.py:trial_compute_table").__module__]


def _ctx(sample_size=10000):
    return SimpleNamespace(session=SimpleNamespace(outputs=None), get_plugin_config=lambda _: {"dataops": {"quality_check": {"sample_size": sample_size}}})


def _write_sql(tmp_path, sql, name="insert_x.sql"):
    path = tmp_path / name
    path.write_text(sql, encoding="utf-8")
    return path


class TestExecution:
    @pytest.mark.asyncio
    async def test_dml_row_limit_matches_sample_size(self, tmp_path):
        """试算的写入量必须被截断到**校验采样量**。

        校验只读前 ``sample_size`` 行，写入更多行既看不到也白等（大分区全量
        写入动辄几十分钟）。
        """
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        seen: list[int | None] = []

        async def fake_validate(
            sql, *, context, _partition_date=None, _collapse_partition_range=False, _row_limit=None
        ):
            seen.append(_row_limit)
            return {"passed": True, "job_id": "j-1"}

        async def fake_quality(sql, *, context, _partition_date=None, row_limit=0):
            return {"ok": True, "warnings": [], "details": {}}

        with (
            patch.object(tct, "dataops_validate_sql", side_effect=fake_validate),
            patch.object(tct, "cleanup_temp_tables", return_value=["j-cleanup"]),
            patch.object(tct, "run_quality_check", side_effect=fake_quality),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), context=_ctx())

        # 默认 sample_size = 10000 → 试算也只写 10000 行
        assert seen == [10000]
        assert result["row_limit"] == 10000


    @pytest.mark.asyncio
    async def test_row_limit_follows_configured_sample_size(self, tmp_path):
        """采样量被配置改过时，试算的截断量要跟着走。"""
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        seen: list[int | None] = []

        async def fake_validate(
            sql, *, context, _partition_date=None, _collapse_partition_range=False, _row_limit=None
        ):
            seen.append(_row_limit)
            return {"passed": True, "job_id": "j-1"}

        async def fake_quality(sql, *, context, _partition_date=None, row_limit=0):
            return {"ok": True, "warnings": [], "details": {}}

        ctx = _ctx(sample_size=500)

        with (
            patch.object(tct, "dataops_validate_sql", side_effect=fake_validate),
            patch.object(tct, "cleanup_temp_tables", return_value=["j-cleanup"]),
            patch.object(tct, "run_quality_check", side_effect=fake_quality),
        ):
            await tct.trial_compute_table(dml_path=str(path), context=ctx)

        assert seen == [500]


    @pytest.mark.asyncio
    async def test_no_row_limit_when_sampling_is_disabled(self, tmp_path):
        """``sample_size <= 0`` = 全量扫描统计 —— 这时不能截断写入。"""
        path = _write_sql(tmp_path, "INSERT OVERWRITE TABLE biads.x SELECT col1 FROM src")

        seen: list[int | None] = []

        async def fake_validate(
            sql, *, context, _partition_date=None, _collapse_partition_range=False, _row_limit=None
        ):
            seen.append(_row_limit)
            return {"passed": True, "job_id": "j-1"}

        async def fake_quality(sql, *, context, _partition_date=None, row_limit=0):
            return {"ok": True, "warnings": [], "details": {}}

        ctx = _ctx(sample_size=0)

        with (
            patch.object(tct, "dataops_validate_sql", side_effect=fake_validate),
            patch.object(tct, "cleanup_temp_tables", return_value=["j-cleanup"]),
            patch.object(tct, "run_quality_check", side_effect=fake_quality),
        ):
            result = await tct.trial_compute_table(dml_path=str(path), context=ctx)

        assert seen == [0]
        assert result["row_limit"] == 0


    @pytest.mark.asyncio
    async def test_ddl_is_not_row_limited(self, tmp_path):
        """DDL 没有数据可写，不该被加 LIMIT。"""
        path = _write_sql(tmp_path, "CREATE TABLE biads.x (col1 string)")

        seen: list[int | None] = []

        async def fake_validate(
            sql, *, context, _partition_date=None, _collapse_partition_range=False, _row_limit=None
        ):
            seen.append(_row_limit)
            return {"passed": True, "job_id": "j-1"}

        with (
            patch.object(tct, "dataops_validate_sql", side_effect=fake_validate),
            patch.object(tct, "cleanup_temp_tables", return_value=["j-cleanup"]),
        ):
            await tct.trial_compute_table(dml_path=str(path), context=_ctx())

        # 建表那一次不带行数上限（DDL 加 LIMIT 是语法错误）
        assert seen == [0]

