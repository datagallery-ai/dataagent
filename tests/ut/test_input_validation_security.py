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
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dataagent.actions.gym.nl2sql.base_env import BaseNL2SQLEnv
from dataagent.actions.gym.ontology_env import OntologyEnv
from dataagent.utils import env_file_loader


def test_duckdb_connection_disables_external_access(monkeypatch, tmp_path):
    db_path = tmp_path / "metadata.duckdb"
    db_path.touch()
    connection = MagicMock()
    connect = MagicMock(return_value=connection)
    monkeypatch.setitem(sys.modules, "duckdb", SimpleNamespace(connect=connect))

    env = BaseNL2SQLEnv(str(db_path))

    assert env.conn is connection
    connect.assert_called_once_with(
        str(db_path),
        read_only=True,
        config={"enable_external_access": "false"},
    )


def test_sql_validation_relies_on_database_external_access_policy():
    env = object.__new__(BaseNL2SQLEnv)
    execute = MagicMock()
    env._execute = execute

    result = env.is_sql_executable("SELECT * FROM read_text('/tmp/secret')")

    assert result["original_msg"] == "OK"
    execute.assert_called_once_with("EXPLAIN SELECT * FROM read_text('/tmp/secret')")


@pytest.mark.parametrize("keyword", ["' OR 1=1 OR '", r"foo\bar", "foo\nbar"])
def test_get_business_procedure_rejects_query_control_characters(keyword):
    env = object.__new__(OntologyEnv)

    with pytest.raises(ValueError, match="keywords"):
        env.get_business_procedure([keyword])


@pytest.mark.parametrize(
    ("line", "expected_log"),
    [
        ("=super-secret", "Could not parse env file key"),
        ('API_KEY="super-secret', "Could not parse value for env key: API_KEY"),
    ],
)
def test_parse_failure_logs_context_without_secret(monkeypatch, line, expected_log):
    messages = []
    monkeypatch.setattr(
        env_file_loader.logger, "warning", lambda message, *args: messages.append(message.format(*args))
    )

    result = env_file_loader._parse_binding_line(line)

    assert result.success is False
    assert messages == [expected_log]
    assert "super-secret" not in messages[0]
