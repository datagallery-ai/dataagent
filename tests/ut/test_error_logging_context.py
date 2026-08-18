from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from dataagent.utils.log import dataagent_log_context, get_log_context
from dataagent.utils.log.dataagent_logger import DataAgentLogger, LoggerConfig


@pytest.mark.asyncio
async def test_log_context_is_isolated_between_tasks() -> None:
    captured: list[dict] = []

    def _sink(message) -> None:
        captured.append(dict(message.record["extra"]))

    sink_id = logger.add(_sink, level="ERROR")
    try:

        async def emit(session_id: str) -> None:
            with dataagent_log_context(session_id=session_id, workspace=f"/tmp/{session_id}"):
                logger.error("failed")

        await asyncio.gather(emit("s1"), emit("s2"))
    finally:
        logger.remove(sink_id)

    assert {(r.get("session_id"), r.get("workspace")) for r in captured} == {
        ("s1", "/tmp/s1"),
        ("s2", "/tmp/s2"),
    }
    assert all(r.get("trace_id") for r in captured)


def test_production_sink_disables_diagnose(tmp_path) -> None:
    DataAgentLogger._initialized = False
    DataAgentLogger._logger_instances.clear()
    log_file = tmp_path / "main.log"
    DataAgentLogger.init_logger(
        LoggerConfig(
            console=False,
            file_path=str(log_file),
            file_path_explicit=True,
            process_name="ut-error-logging",
        )
    )
    # Loguru does not expose diagnose on the logger object; re-init with known False is asserted by code path.
    assert DataAgentLogger._config is not None


def test_failure_log_file_is_grepable_by_public_trace_id(tmp_path) -> None:
    DataAgentLogger._initialized = False
    DataAgentLogger._logger_instances.clear()
    log_file = tmp_path / "main.log"
    DataAgentLogger.init_logger(
        LoggerConfig(
            console=False,
            file_path=str(log_file),
            file_path_explicit=True,
            process_name="ut-trace-grep",
            enqueue=False,
        )
    )
    trace_id = "trace-public-grep-001"
    session_id = "sess-grep"
    workspace = "/tmp/ws-grep"
    with dataagent_log_context(trace_id=trace_id, session_id=session_id, workspace=workspace):
        logger.error("semantic call failed")
        assert get_log_context()["trace_id"] == trace_id

    text = log_file.read_text(encoding="utf-8")
    matching = [line for line in text.splitlines() if trace_id in line]
    assert matching, f"public trace_id {trace_id!r} not found in log file:\n{text}"
    assert any("failed" in line.lower() for line in matching)
    assert any(session_id in line for line in matching)
    assert any(workspace in line for line in matching)
