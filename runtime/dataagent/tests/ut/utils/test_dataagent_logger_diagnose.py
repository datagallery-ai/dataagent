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
"""LoggerConfig.diagnose defaults on; DATAAGENT_LOG_DIAGNOSE can disable."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.utils.log import dataagent_logger as logmod


@pytest.fixture(autouse=True)
def _reset_logger_state():
    logmod.DataAgentLogger._initialized = False
    logmod.DataAgentLogger._logger_instances.clear()
    logmod.DataAgentLogger._config = None
    logmod.DataAgentLogger._logger = None
    logmod.logger = None
    yield
    logmod.DataAgentLogger._initialized = False
    logmod.DataAgentLogger._logger_instances.clear()
    logmod.DataAgentLogger._config = None
    logmod.DataAgentLogger._logger = None
    logmod.logger = None
    from loguru import logger as _loguru_logger

    _loguru_logger.remove()


def _handler_diagnose_flags() -> list[bool]:
    from loguru import logger as _loguru

    return [handler._exception_formatter._diagnose for handler in _loguru._core.handlers.values()]


def _handler_backtrace_flags() -> list[bool]:
    from loguru import logger as _loguru

    return [handler._exception_formatter._backtrace for handler in _loguru._core.handlers.values()]


def test_diagnose_defaults_true_and_env_can_disable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATAAGENT_LOG_DIAGNOSE", raising=False)
    assert logmod.LoggerConfig().diagnose is True

    logmod.reconfigure(
        logmod.LoggerConfig(
            file_path=str(tmp_path / "logs-default" / "main.log"),
            file_path_explicit=True,
            console=True,
            enqueue=False,
        )
    )
    default_flags = _handler_diagnose_flags()
    assert default_flags
    assert all(default_flags)
    assert all(_handler_backtrace_flags())

    for raw in ("false", "0", "no", "off"):
        monkeypatch.setenv("DATAAGENT_LOG_DIAGNOSE", raw)
        logmod.reconfigure(
            logmod.LoggerConfig(
                file_path=str(tmp_path / f"logs-{raw}" / "main.log"),
                file_path_explicit=True,
                console=True,
                enqueue=False,
            )
        )
        assert logmod.DataAgentLogger._config is not None
        assert logmod.DataAgentLogger._config.diagnose is False

    monkeypatch.setenv("DATAAGENT_LOG_DIAGNOSE", "false")
    logmod.reconfigure(
        logmod.LoggerConfig(
            file_path=str(tmp_path / "logs-off" / "main.log"),
            file_path_explicit=True,
            console=True,
            enqueue=False,
        )
    )
    off_flags = _handler_diagnose_flags()
    assert off_flags
    assert all(flag is False for flag in off_flags)
    assert all(_handler_backtrace_flags())

    for raw in ("true", "1", "yes"):
        monkeypatch.setenv("DATAAGENT_LOG_DIAGNOSE", raw)
        logmod.reconfigure(
            logmod.LoggerConfig(
                file_path=str(tmp_path / f"logs-on-{raw}" / "main.log"),
                file_path_explicit=True,
                console=False,
                enqueue=False,
            )
        )
        assert logmod.DataAgentLogger._config is not None
        assert logmod.DataAgentLogger._config.diagnose is True


def test_diagnose_fallback_stderr_uses_same_config(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    logmod.reconfigure(
        logmod.LoggerConfig(
            file_path=str(blocker / "logs" / "main.log"),
            file_path_explicit=True,
            console=False,
            diagnose=False,
        )
    )
    flags = _handler_diagnose_flags()
    assert flags
    assert all(flag is False for flag in flags)
    assert all(_handler_backtrace_flags())
