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
"""Target-table extraction cases from main !561."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"
LOADER = PythonLoader()
vt = sys.modules[
    LOADER.load(PLUGIN, "utils/dataops_validate_tool.py:extract_sql_target_table").__module__
]


class TestExtractSqlTargetTable:
    """extract_sql_target_table 返回**真实**目标表（不改写成临时表）。"""

    def test_create_table(self):
        sql = "CREATE EXTERNAL TABLE IF NOT EXISTS biads.ads_x (a STRING) STORED AS ORC"
        assert vt.extract_sql_target_table(sql) == "biads.ads_x"

    def test_create_table_without_external(self):
        sql = "CREATE TABLE IF NOT EXISTS biads.ads_x (a STRING)"
        assert vt.extract_sql_target_table(sql) == "biads.ads_x"

    def test_insert_overwrite(self):
        sql = "INSERT OVERWRITE TABLE biads.ads_y PARTITION (pt_d='x') SELECT 1"
        assert vt.extract_sql_target_table(sql) == "biads.ads_y"

    def test_insert_without_table_keyword(self):
        sql = "INSERT OVERWRITE biads.ads_y PARTITION (pt_d='x') SELECT 1"
        assert vt.extract_sql_target_table(sql) == "biads.ads_y"

    def test_select_returns_none(self):
        assert vt.extract_sql_target_table("SELECT 1 FROM biads.x") is None

    def test_empty_returns_none(self):
        assert vt.extract_sql_target_table("") is None

    def test_ignores_temp_table_names(self):
        """临时表名不应被当成真实目标表返回。"""
        sql = "INSERT OVERWRITE TABLE adhoctemp.tmp_u1_20270101_ads_y SELECT 1"
        assert vt.extract_sql_target_table(sql) == "adhoctemp.tmp_u1_20270101_ads_y"


class TestAnalyzeLogDirectly:
    def test_prompt_states_sql_is_the_rewritten_submitted_version(self):
        collected_stream = AsyncMock()
        """The analyzer must be told the log shows the rewritten SQL.

        The SQL executed by DataOps has the target table replaced with a temp
        table name and CTEs inlined as subqueries. Without that hint the model
        reports the rewritten temp table as a missing/typo'd table.
        """
        captured_prompt = {}

        def _capture_stream(_model, messages):
            captured_prompt["content"] = messages[1].get("content", "")
            return '{"error_type":"AnalysisException","error_message":"x","location":{},"suggestions":[]}'

        llm = object()
        collected_stream.side_effect = _capture_stream
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": "error log"},
            ),
            patch.object(vt, "invoke_model_text", collected_stream),
        ):
            asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-rewritten",
                    raw_error="x",
                    context=SimpleNamespace(
                        get_model=lambda _: llm, get_plugin_config=lambda _: {}
                    ),
                )
            )

        content = captured_prompt.get("content", "")
        assert "adhoctemp.tmp_*" in content
        assert "CTEs have been inlined" in content
        assert "temp table name" in content

    def test_prompt_names_the_source_file_and_table_mapping(self):
        collected_stream = AsyncMock()
        """The analyzer must be told which file the executed SQL came from.

        The submitted SQL is a rewritten copy of a workspace file (temp table +
        inlined CTEs). Without the file name and the real-table → temp-table
        mapping the model cannot point the agent at the right file to fix.
        """
        captured_prompt = {}

        def _capture_stream(_model, messages):
            captured_prompt["content"] = messages[1].get("content", "")
            return '{"error_type":"AnalysisException","error_message":"x","location":{},"suggestions":[]}'

        llm = object()
        collected_stream.side_effect = _capture_stream
        with (
            patch.object(
                vt,
                "dataops_fetch_obs_log",
                return_value={"status": "success", "log_content": "error log"},
            ),
            patch.object(vt, "invoke_model_text", collected_stream),
        ):
            asyncio.run(
                vt._analyze_log_directly(
                    log_file_info={"url": "https://x", "headers": {"a": "b"}},
                    job_id="job-rewritten",
                    raw_error="x",
                    context=SimpleNamespace(
                        get_model=lambda _: llm, get_plugin_config=lambda _: {}
                    ),
                    source_file="insert_ads_result.sql",
                    original_target_table="biads.ads_result",
                    rewritten_target_table="adhoctemp.tmp_u1_20270101_ads_result",
                )
            )

        content = captured_prompt.get("content", "")
        # 告诉 LLM 报错对应哪个文件
        assert "insert_ads_result.sql" in content
        assert "REWRITTEN copy" in content
        # 告诉 LLM 真实表名和临时表名的对应关系
        assert "biads.ads_result" in content
        assert "adhoctemp.tmp_u1_20270101_ads_result" in content
        assert "map it back" in content
