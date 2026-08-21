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
from unittest.mock import AsyncMock, Mock

import pytest

from dataagent.agents.nl2sql.nodes.validator import ValidatorNode
from dataagent.agents.nl2sql.workflow.state import Result
from dataagent.config.config_manager import ConfigManager


def _config_manager() -> ConfigManager:
    manager = ConfigManager()
    manager.settings = {"DATABASE": {"dialect": "postgres", "engine": "gaussvector", "config": {}}}
    return manager


@pytest.mark.asyncio
async def test_validator_security_switch_defaults_to_disabled() -> None:
    """Validator should preserve the current behavior when the new switch is omitted."""
    node = ValidatorNode(config_manager=_config_manager(), db_explain=False)
    candidates = [Result(id=0, sql="SELECT current_setting('search_path')")]

    result = await node._validate_syntax(candidates, {})

    assert result[0].get("score", 0) == 1
    assert candidates[0].security_violations == []


@pytest.mark.asyncio
async def test_validator_records_candidate_security_violations() -> None:
    """Validator should attach structured violations to each blocked candidate."""
    node = ValidatorNode(config_manager=_config_manager(), db_explain=False, sql_security_enabled=True)
    candidates = [Result(id=0, sql="SELECT current_setting('search_path')")]

    result = await node._validate_syntax(candidates, {"orders": {"columns": {"id": {}}}})

    candidate = candidates[0]
    assert result[0].get("score", 1) == 0
    assert candidate.security_checked is True
    assert candidate.security_violations[0].get("rule_id", "") == "FUNCTION-001"
    assert "FUNCTION-001" in result[0].get("issues", [""])[0]


@pytest.mark.asyncio
async def test_validator_security_check_replaces_legacy_sqlglot_parse() -> None:
    """Enabled security validation should parse each candidate only through the security module."""
    node = ValidatorNode(config_manager=_config_manager(), db_explain=False, sql_security_enabled=True)
    node._validate_with_sqlglot = Mock(return_value=[])
    candidates = [Result(id=0, sql="SELECT id FROM orders WHERE id = 1")]

    await node._validate_syntax(candidates, {"orders": {"columns": {"id": {}}}})

    node._validate_with_sqlglot.assert_not_called()


@pytest.mark.asyncio
async def test_validator_uses_semantically_normalized_sql() -> None:
    """Validator should retain the safe quoted form produced by semantic identifier resolution."""
    node = ValidatorNode(config_manager=_config_manager(), db_explain=False, sql_security_enabled=True)
    candidates = [
        Result(
            id=0,
            sql=(
                "WITH aligned_periods AS ("
                "SELECT time AS current_time FROM orders WHERE id = 1"
                ") SELECT current_time FROM aligned_periods ORDER BY current_time"
            ),
        )
    ]
    schema = {"orders": {"columns": {"id": {}, "time": {}}}}

    result = await node._validate_syntax(candidates, schema)

    assert result[0].get("score", 0) == 1
    assert candidates[0].sql.count('"current_time"') == 3


@pytest.mark.asyncio
async def test_validator_skips_explain_for_security_blocked_candidate() -> None:
    """Validator should never EXPLAIN a candidate blocked by the security module."""
    node = ValidatorNode(config_manager=_config_manager(), db_explain=True, sql_security_enabled=True)
    node._validate_with_db_explain = AsyncMock(return_value=[])
    candidates = [Result(id=0, sql="SELECT current_setting('search_path')")]

    await node._validate_syntax(candidates, {})

    node._validate_with_db_explain.assert_not_awaited()


@pytest.mark.asyncio
async def test_validator_checks_all_generated_candidates_independently() -> None:
    """Multi-candidate validation should retain a safe candidate while marking blocked siblings."""
    node = ValidatorNode(config_manager=_config_manager(), db_explain=False, sql_security_enabled=True)
    candidates = [
        Result(id=0, sql="SELECT current_setting('search_path')"),
        Result(id=1, sql="SELECT id FROM orders WHERE id = 1"),
    ]

    result = await node._validate_syntax(candidates, {"orders": {"columns": {"id": {}}}})

    blocked, safe = candidates
    assert blocked.security_violations
    assert blocked.security_checked is True
    assert safe.security_violations == []
    assert safe.security_checked is True
    assert result[1].get("score", 0) == 1
