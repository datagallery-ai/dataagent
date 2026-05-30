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
"""natural_language_to_sql must forward _tool_context to load_table."""

from unittest.mock import MagicMock, patch

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.config.config_manager import ConfigManager


def test_natural_language_to_sql_forwards_tool_context_to_load_table():
    """load_table receives the same _tool_context as the parent tool."""
    from dataagent.actions.tools.local_tool.tools import natural_language_to_sql

    cm = ConfigManager()
    cm.set("DATASOURCE.database_address", "mysql+pymysql://u:p@h/db")
    cm.set("DATASOURCE.database_table_name", "t")
    ctx = ToolExecutionContext(config_manager=cm)

    captured: dict = {}

    def _fake_load_table(sql_command: str, *, _tool_context: ToolExecutionContext):
        captured["sql"] = sql_command
        captured["ctx"] = _tool_context
        import pandas as pd

        return pd.DataFrame({"value": [1]})

    fake_llm = MagicMock()
    fake_llm.invoke.return_value = MagicMock(
        content="```mysql\nSELECT 1 AS value\n```",
        usage_metadata={"total_tokens": 1},
    )

    with (
        patch("dataagent.actions.tools.local_tool.tools.llm_manager.get_default_llm", return_value=fake_llm),
        patch("dataagent.actions.tools.local_tool.tools.load_table", side_effect=_fake_load_table),
        patch("dataagent.actions.tools.local_tool.tools._resolve_tool_file_path", side_effect=lambda p, _n: p),
    ):
        result = natural_language_to_sql(
            query="test",
            data_schema="t: []",
            sql_save_path="/tmp/q.sql",
            csv_save_path="/tmp/q.csv",
            _tool_context=ctx,
        )

    assert captured["ctx"] is ctx
    assert "SELECT" in captured["sql"]
    assert "SQL 文件已保存到" in result["frontend_msg"]
