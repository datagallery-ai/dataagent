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
from __future__ import annotations

from typing import Any

import pytest

from dataagent.agents.nl2sql.nodes import executor as executor_module
from dataagent.agents.nl2sql.nodes import selector as selector_module
from dataagent.agents.nl2sql.nodes.executor import ExecutorNode
from dataagent.agents.nl2sql.nodes.selector import SelectorNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import sql_sha256
from dataagent.agents.nl2sql.workflow.state import Result, get_default_state


class _Config:
    def get(self, _key: str, default: Any = None) -> Any:
        return default


def _capture_info(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[str]:
    messages: list[str] = []

    def capture(message: str, *args: Any) -> None:
        messages.append(message.format(*args))

    monkeypatch.setattr(module.logger, "info", capture)
    return messages


def test_sql_sha256_normalizes_whitespace() -> None:
    assert sql_sha256("  SELECT   secret\nFROM account  ") == sql_sha256("SELECT secret FROM account")


@pytest.mark.asyncio
async def test_selector_info_logs_only_safe_result_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    sql_secret = "SELECT private_token FROM customer_secret"
    failed_sql_secret = "SELECT password FROM credential_secret"
    row_secret = "ROW_SECRET_7f5f"
    error_secret = "ERROR_SECRET_91ab"
    successful = Result(
        id=1,
        sql=sql_secret,
        rows=[(row_secret,)],
        rows_preview=[(row_secret,)],
        columns=["private_token"],
    )
    failed = Result(id=2, sql=failed_sql_secret, rows=None, rows_preview=None, error=error_secret)
    state = get_default_state(
        "sensitive question",
        execution_results=[successful, failed],
        sel_retries=1,
    )
    messages = _capture_info(monkeypatch, selector_module)

    result = await SelectorNode(shortcut=1, threshold=0.5)._aprocess(state)

    log_text = "\n".join(messages)
    assert sql_secret not in log_text
    assert failed_sql_secret not in log_text
    assert row_secret not in log_text
    assert error_secret not in log_text
    assert f"sql_sha256={sql_sha256(sql_secret)}" in log_text
    assert f"sql_sha256={sql_sha256(failed_sql_secret)}" in log_text
    assert "row_count=1" in log_text
    assert "error_code=NONE" in log_text
    assert "error_code=EXECUTION_ERROR" in log_text
    assert result["sql"] == sql_secret
    assert row_secret in result["stream_message"]


@pytest.mark.asyncio
async def test_executor_info_logs_only_safe_execution_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    sql_secret = "SELECT ssn FROM person_secret"
    failed_sql_secret = "SELECT api_key FROM service_secret"
    row_secret = "ROW_SECRET_ef13"
    error_secret = "ERROR_SECRET_240c"
    state = get_default_state(
        "sensitive question",
        validation_results=[
            Result(id=3, sql=sql_secret),
            Result(id=4, sql=failed_sql_secret),
        ],
    )
    node = ExecutorNode(config_manager=_Config())
    monkeypatch.setattr(
        node,
        "_execute_queries",
        lambda _config, _sqls: [
            (["ssn"], [(row_secret,)], None),
            (None, None, error_secret),
        ],
    )
    messages = _capture_info(monkeypatch, executor_module)

    result = await node._aprocess(state)

    log_text = "\n".join(messages)
    assert sql_secret not in log_text
    assert failed_sql_secret not in log_text
    assert row_secret not in log_text
    assert error_secret not in log_text
    assert f"sql_sha256={sql_sha256(sql_secret)}" in log_text
    assert f"sql_sha256={sql_sha256(failed_sql_secret)}" in log_text
    assert "row_count=1" in log_text
    assert "error_code=NONE" in log_text
    assert "error_code=EXECUTION_ERROR" in log_text
    assert row_secret in result["stream_message"]
