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
"""External-behavior contracts for br0930 per-PID logger."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import zipfile
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
    logmod._session_context_var.set(logmod._NO_SESSION)
    yield
    logmod.DataAgentLogger._initialized = False
    logmod.DataAgentLogger._logger_instances.clear()
    logmod.DataAgentLogger._config = None
    logmod.DataAgentLogger._logger = None
    logmod.logger = None
    logmod._session_context_var.set(logmod._NO_SESSION)
    from loguru import logger as _loguru_logger

    _loguru_logger.remove()


def _active(log_dir: Path) -> Path:
    return log_dir / f"main.{os.getpid()}.log"


def _managed_history(log_dir: Path) -> list[Path]:
    return [
        p
        for p in log_dir.iterdir()
        if p.is_file() and logmod.is_managed_log_filename(p.name) and not logmod.is_live_active_log_file(p)
    ]


def _complete() -> None:
    from loguru import logger as _loguru

    _loguru.complete()


def test_log_dir_defaults_and_custom_and_ignores_log_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    monkeypatch.delenv("DATAAGENT_LOG_PATH", raising=False)
    monkeypatch.setenv("DATAAGENT_LOG_FILE", str(tmp_path / "ignored.log"))

    logmod.reconfigure(logmod.build_config_from_env())
    default_dir = (home / "logs").resolve()
    assert Path(logmod.DataAgentLogger._config.log_path).resolve() == default_dir
    assert (default_dir / f"main.{os.getpid()}.log").exists()
    assert not (tmp_path / "ignored.log").exists()

    custom = tmp_path / "custom-logs"
    monkeypatch.setenv("DATAAGENT_LOG_PATH", str(custom))
    logmod.reconfigure(logmod.build_config_from_env())
    assert _active(custom).exists()


def test_main_pid_file_shared_across_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("DATAAGENT_LOG_PATH", str(log_dir))
    monkeypatch.setenv("DATAAGENT_LOG_CONSOLE", "false")
    logmod.reconfigure(logmod.build_config_from_env())
    logger = logmod.get_logger()
    for sid, rid in (("A", 1), ("B", 2)):
        token = logmod.set_session_log_context(sid, rid)
        try:
            logger.info(f"req-{sid}")
        finally:
            logmod.reset_session_log_context(token)
    _complete()
    text = _active(log_dir).read_text(encoding="utf-8")
    assert "req-A" in text and "session=A" in text and "run=1" in text
    assert "req-B" in text and "session=B" in text and "run=2" in text
    assert list(log_dir.glob("main.*.log")) == [_active(log_dir)]


def test_rotation_defaults_override_and_zip_openable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATAAGENT_LOG_ROTATION", raising=False)
    assert logmod.build_config_from_env().rotation == "100 MB"

    log_dir = tmp_path / "logs"
    monkeypatch.setenv("DATAAGENT_LOG_PATH", str(log_dir))
    monkeypatch.setenv("DATAAGENT_LOG_ROTATION", "1 KB")
    monkeypatch.setenv("DATAAGENT_LOG_CONSOLE", "false")
    monkeypatch.setenv("DATAAGENT_LOG_RETENTION_COUNT", "20")
    logmod.reconfigure(logmod.build_config_from_env())
    logger = logmod.get_logger()
    payload = "x" * 512
    for _ in range(30):
        logger.info(payload)
    _complete()

    zips = [p for p in log_dir.iterdir() if p.suffix == ".zip" and logmod.is_managed_log_filename(p.name)]
    assert zips
    for zpath in zips:
        with zipfile.ZipFile(zpath, "r") as zf:
            assert zf.testzip() is None
            assert zf.namelist()


def test_global_retention_keeps_n_across_pids(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for i in range(5):
        p = log_dir / f"main.{1000 + i}.2020-01-01_00-00-00_{i:06d}.log.zip"
        p.write_text(f"h{i}", encoding="utf-8")
        os.utime(p, (1_000 + i, 1_000 + i))
    logmod.enforce_log_retention(log_dir, retention_count=2)
    managed = [p for p in log_dir.iterdir() if logmod.is_managed_log_filename(p.name)]
    assert len(managed) == 2
    assert "main.1004.2020-01-01_00-00-00_000004.log.zip" in {p.name for p in managed}


def test_live_dead_pid_and_non_log_protection(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    live = log_dir / f"main.{os.getpid()}.log"
    live.write_text("live", encoding="utf-8")
    dead = log_dir / "main.99999.log"
    dead.write_text("dead", encoding="utf-8")
    os.utime(dead, (1, 1))
    hist = log_dir / "main.88888.2020-01-01_00-00-00_000000.log.zip"
    hist.write_text("hist", encoding="utf-8")
    os.utime(hist, (2, 2))
    keep = [log_dir / "unrelated.db", log_dir / "README", log_dir / ".hidden"]
    for path in keep:
        path.write_text("keep", encoding="utf-8")

    logmod.enforce_log_retention(log_dir, retention_count=1)
    assert live.exists()
    assert not dead.exists()
    assert hist.exists()
    for path in keep:
        assert path.exists()


def _mp_rotate_worker(log_dir: str, retention_count: int, done) -> None:
    os.environ["DATAAGENT_LOG_PATH"] = log_dir
    os.environ["DATAAGENT_LOG_ROTATION"] = "1 KB"
    os.environ["DATAAGENT_LOG_RETENTION_COUNT"] = str(retention_count)
    os.environ["DATAAGENT_LOG_CONSOLE"] = "false"
    from dataagent.utils.log import dataagent_logger as child_log

    child_log.DataAgentLogger._initialized = False
    child_log.DataAgentLogger._logger_instances.clear()
    child_log.reconfigure(child_log.build_config_from_env())
    logger = child_log.get_logger()
    payload = "y" * 400
    for _ in range(40):
        logger.info(payload)
    from loguru import logger as _loguru

    _loguru.complete()
    done.set()


def test_two_processes_rotate_zip_and_converge_global_n(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "unrelated.db").write_text("db", encoding="utf-8")
    for i in range(8):
        p = log_dir / f"main.{3000 + i}.2020-01-01_00-00-00_{i:06d}.log.zip"
        p.write_text(f"seed-{i}", encoding="utf-8")
        os.utime(p, (i + 1, i + 1))

    retention = 4
    d1 = multiprocessing.Event()
    d2 = multiprocessing.Event()
    p1 = multiprocessing.Process(target=_mp_rotate_worker, args=(str(log_dir), retention, d1))
    p2 = multiprocessing.Process(target=_mp_rotate_worker, args=(str(log_dir), retention, d2))
    p1.start()
    p2.start()
    assert d1.wait(40) and d2.wait(40)
    p1.join(15)
    p2.join(15)
    assert p1.exitcode == 0 and p2.exitcode == 0

    logmod.enforce_log_retention(log_dir, retention_count=retention)
    assert (log_dir / "unrelated.db").exists()
    history = _managed_history(log_dir)
    assert len(history) <= retention
    for zpath in history:
        if zpath.suffix == ".zip":
            with zipfile.ZipFile(zpath, "r") as zf:
                assert zf.testzip() is None


def test_session_run_defaults_and_async_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("DATAAGENT_LOG_PATH", str(log_dir))
    monkeypatch.setenv("DATAAGENT_LOG_CONSOLE", "false")
    logmod.reconfigure(logmod.build_config_from_env())
    logger = logmod.get_logger()
    logger.info("no-ctx")
    _complete()
    text = _active(log_dir).read_text(encoding="utf-8")
    assert "session=-" in text and "run=0" in text

    async def _one(sid: str, rid: int):
        token = logmod.set_session_log_context(sid, rid)
        try:
            logger.info(f"async-{sid}")
            await asyncio.sleep(0.02)
        finally:
            logmod.reset_session_log_context(token)

    async def _both():
        await asyncio.gather(_one("S1", 11), _one("S2", 22))

    asyncio.run(_both())
    _complete()
    text2 = _active(log_dir).read_text(encoding="utf-8")
    assert "session=S1" in text2 and "run=11" in text2 and "async-S1" in text2
    assert "session=S2" in text2 and "run=22" in text2 and "async-S2" in text2


def test_chat_success_and_exception_reset_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAAGENT_LOG_PATH", str(tmp_path / "logs"))
    monkeypatch.setenv("DATAAGENT_LOG_CONSOLE", "false")
    logmod.reconfigure(logmod.build_config_from_env())

    from dataagent.interface.sdk import agent as sdk_agent_module
    from dataagent.interface.sdk.agent import DataAgent

    class _Ok:
        async def chat(self, *args, **kwargs):
            logmod.get_logger().info("inside-chat")
            return {"ok": True}

    class _Boom:
        async def chat(self, *args, **kwargs):
            raise RuntimeError("chat-boom")

    def _probe(chat_agent):
        agent = object.__new__(DataAgent)
        agent.config = type("_C", (), {"get": lambda self, k, d=None: d, "get_all": lambda self: {}})()
        agent._chat_agent_instance = chat_agent
        agent._validate_workspace = lambda workspace: workspace
        agent._initialize_state = lambda initial_state, session_id, workspace: {
            "user_id": "u",
            "session_id": "CHAT-S",
            "run_id": 3,
            "workspace": tmp_path,
            **(initial_state or {}),
        }
        agent._ensure_workspace = lambda state: None
        agent._touch_workspace_catalog = lambda state: None
        agent._dump_runtime_config = lambda state: None
        monkeypatch.setattr(sdk_agent_module, "logger", logmod.get_logger())
        return agent

    async def _ok():
        agent = _probe(_Ok())
        result = await agent.chat("hi", session_id="CHAT-S", initial_state={"run_id": 3})
        assert result == {"ok": True}
        assert logmod._session_context_var.get().session_id is None

    async def _err():
        agent = _probe(_Boom())
        result = await agent.chat("hi", session_id="CHAT-S", initial_state={"run_id": 3})
        assert "error" in result
        assert logmod._session_context_var.get().session_id is None

    asyncio.run(_ok())
    asyncio.run(_err())
    _complete()
    text = _active(tmp_path / "logs").read_text(encoding="utf-8")
    assert "session=CHAT-S" in text and "inside-chat" in text


def test_astream_same_cross_task_early_close_and_error_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAAGENT_LOG_PATH", str(tmp_path / "logs"))
    monkeypatch.setenv("DATAAGENT_LOG_CONSOLE", "false")
    logmod.reconfigure(logmod.build_config_from_env())

    from dataagent.interface.sdk import agent as sdk_agent_module
    from dataagent.interface.sdk.agent import DataAgent

    def _make(*, boom: bool = False):
        class _Chat:
            def astream(self, *args, **kwargs):
                async def _gen():
                    if boom:
                        raise RuntimeError("stream-boom")
                    logmod.get_logger().info("stream-chunk")
                    yield {"chunk": 1}
                    yield {"chunk": 2}

                return _gen()

        agent = object.__new__(DataAgent)
        agent.config = type("_C", (), {"get": lambda self, k, d=None: d, "get_all": lambda self: {}})()
        agent._chat_agent_instance = _Chat()
        agent._validate_workspace = lambda workspace: workspace
        agent._initialize_state = lambda initial_state, session_id, workspace: {
            "user_id": "u",
            "session_id": "AST-S",
            "run_id": 9,
            "workspace": tmp_path,
            **(initial_state or {}),
        }

        def _ensure(state):
            logmod.get_logger().info("pre-ensure")

        agent._ensure_workspace = _ensure
        agent._touch_workspace_catalog = lambda state: None
        agent._dump_runtime_config = lambda state: None
        monkeypatch.setattr(sdk_agent_module, "logger", logmod.get_logger())
        return agent

    async def _same_task():
        stream = _make().astream(input={}, session_id="AST-S", initial_state={"run_id": 9})
        assert logmod._session_context_var.get().session_id is None
        assert [x async for x in stream] == [{"chunk": 1}, {"chunk": 2}]
        assert logmod._session_context_var.get().session_id is None

    async def _cross_task():
        stream = _make().astream(input={}, session_id="AST-S", initial_state={"run_id": 9})
        assert logmod._session_context_var.get().session_id is None

        async def _consume():
            return [x async for x in stream]

        assert await asyncio.create_task(_consume()) == [{"chunk": 1}, {"chunk": 2}]
        assert logmod._session_context_var.get().session_id is None

    async def _early():
        stream = _make().astream(input={}, session_id="AST-S", initial_state={"run_id": 9})
        assert (await stream.__anext__()) == {"chunk": 1}
        await stream.aclose()
        assert logmod._session_context_var.get().session_id is None

    async def _boom():
        stream = _make(boom=True).astream(input={}, session_id="AST-S", initial_state={"run_id": 9})
        with pytest.raises(RuntimeError, match="stream-boom"):
            await stream.__anext__()
        assert logmod._session_context_var.get().session_id is None

    asyncio.run(_same_task())
    asyncio.run(_cross_task())
    asyncio.run(_early())
    asyncio.run(_boom())
    _complete()
    text = _active(tmp_path / "logs").read_text(encoding="utf-8")
    assert "session=AST-S" in text and "pre-ensure" in text and "stream-chunk" in text


def test_retention_count_invalid_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATAAGENT_LOG_RETENTION_COUNT", raising=False)
    assert logmod.build_config_from_env().retention_count == 20
    monkeypatch.setenv("DATAAGENT_LOG_RETENTION_COUNT", "7")
    assert logmod.build_config_from_env().retention_count == 7
    for bad in ("0", "-1", "abc"):
        monkeypatch.setenv("DATAAGENT_LOG_RETENTION_COUNT", bad)
        with pytest.raises(ValueError):
            logmod.build_config_from_env()


def test_get_env_config_ignores_dataagent_log_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧 LOG_FILE 视为不存在：get_env_config 不读、不返回该键。"""
    from dataagent.utils.log.config import LogConfig

    monkeypatch.setenv("DATAAGENT_LOG_FILE", "/tmp/should-not-appear.log")
    monkeypatch.setenv("DATAAGENT_LOG_LEVEL", "WARNING")
    cfg = LogConfig.get_env_config()
    assert "file_path" not in cfg
    assert "DATAAGENT_LOG_FILE" not in cfg
    assert cfg.get("level") == "WARNING"
    assert all("LOG_FILE" not in str(k).upper() or k == "level" for k in cfg)
    # Stronger: no value equals the planted path.
    assert "/tmp/should-not-appear.log" not in {str(v) for v in cfg.values() if v is not None}


def test_logger_config_legacy_fields_constructible_but_ignored_for_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline LoggerConfig kwargs remain constructible; file_path does not route sinks."""
    planted = tmp_path / "legacy-explicit.log"
    cfg = logmod.LoggerConfig(
        file_path=str(planted),
        file_path_explicit=True,
        retention="1 day",
        compression="gz",
        json_logs=True,
        log_path=str(tmp_path / "logs"),
        console=False,
    )
    assert cfg.file_path == str(planted)
    assert cfg.file_path_explicit is True
    assert cfg.retention == "1 day"
    assert cfg.compression == "gz"
    assert cfg.json_logs is True

    monkeypatch.setenv("DATAAGENT_LOG_PATH", str(tmp_path / "logs"))
    logmod.reconfigure(cfg)
    logmod.get_logger().info("legacy-fields-ignored")
    _complete()

    assert not planted.exists()
    assert _active(tmp_path / "logs").exists()


def test_crash_leftover_rotated_raw_is_zipped_or_pruned(tmp_path: Path) -> None:
    """Rename-after-crash raw must not accumulate forever under retention=1."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    older = log_dir / "main.1001.2020-01-01_00-00-00_000001.log.zip"
    older.write_text("older", encoding="utf-8")
    os.utime(older, (1_577_836_800, 1_577_836_800))  # 2020-01-01
    leftover = log_dir / "main.1002.2020-01-01_00-00-00_000002.log"
    leftover.write_text("crash-leftover-raw", encoding="utf-8")
    os.utime(leftover, (1_577_836_801, 1_577_836_801))
    (log_dir / "unrelated.db").write_text("db", encoding="utf-8")

    logmod.enforce_log_retention(log_dir, retention_count=1)

    assert (log_dir / "unrelated.db").exists()
    assert not leftover.exists(), "raw leftover should be zipped away"
    zipped = log_dir / f"{leftover.name}.zip"
    history = _managed_history(log_dir)
    assert len(history) == 1
    assert history[0].name.endswith(".zip")
    if zipped.exists():
        with zipfile.ZipFile(zipped, "r") as zf:
            assert zf.testzip() is None
            payload = b"".join(zf.read(n) for n in zf.namelist())
            assert b"crash-leftover-raw" in payload


def test_zip_failure_keeps_raw_in_global_retention_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for i in range(3):
        raw = log_dir / f"main.{2000 + i}.2020-01-01_00-00-00_{i:06d}.log"
        raw.write_text(f"raw-{i}", encoding="utf-8")
        os.utime(raw, (1_577_836_800 + i, 1_577_836_800 + i))

    def _fail_zip(path: Path) -> Path | None:
        raise OSError("zip unavailable")

    monkeypatch.setattr(logmod, "_zip_rotated_raw", _fail_zip)
    logmod.enforce_log_retention(log_dir, retention_count=1)

    remaining = _managed_history(log_dir)
    assert len(remaining) == 1
    assert remaining[0].suffix == ".log"
    assert "raw-2" in remaining[0].read_text(encoding="utf-8")
