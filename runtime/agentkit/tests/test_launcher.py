import json
import os
import socket
import subprocess
import sys
import threading
from uuid import uuid4

import pytest
from process_helpers import stop_child, wait_ready

from serve.__main__ import _listen, _parser, _prepare, check_port, runtime_url
from serve.sessions import workspace_lock


def open_listener():
    """Bind an ephemeral port and keep listening until the child inherits the socket."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def spawn_inherited(argv, listener, *, env, fd_env, **kwargs):
    """Hand a still-open listener to the child, then close this process's copy."""
    forwarded = {**env, fd_env: str(listener.fileno())}
    try:
        return subprocess.Popen(argv, env=forwarded, pass_fds=(listener.fileno(),), **kwargs)
    finally:
        listener.close()


def config_file(tmp_path, listener=None):
    if listener is None:
        # System tests still release the port and let the TUI bind it. They stay serial.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    else:
        port = listener.getsockname()[1]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "models": {"primary": {
            "name": "process-test", "base_url": "https://example.invalid/v1",
            "api_key": "fake-test-key",
        }},
        "server": {"port": port},
    }))
    return path, port


def test_ready_url_brackets_ipv6_bind_addresses():
    assert runtime_url("127.0.0.1", 8790) == "http://127.0.0.1:8790/api/v1/runs"
    assert runtime_url("0.0.0.0", 8790) == "http://0.0.0.0:8790/api/v1/runs"
    assert runtime_url("::", 8790) == "http://[::]:8790/api/v1/runs"
    assert runtime_url("::0", 8790) == "http://[::0]:8790/api/v1/runs"
    assert runtime_url("::1", 8790) == "http://[::1]:8790/api/v1/runs"
    assert runtime_url("[::1]", 8790) == "http://[::1]:8790/api/v1/runs"


def test_occupied_port_is_untouched():
    with socket.socket() as original:
        original.bind(("127.0.0.1", 0))
        original.listen()
        port = original.getsockname()[1]
        with pytest.raises(RuntimeError, match="occupied"):
            check_port("127.0.0.1", port)
        assert original.fileno() >= 0


def test_workspace_lock_is_live_os_lock_not_stale_file(tmp_path):
    with workspace_lock(tmp_path):
        with pytest.raises(RuntimeError, match="owns this workspace"):
            with workspace_lock(tmp_path):
                pass
    assert (tmp_path / "backend.lock").exists()
    with workspace_lock(tmp_path):
        pass


def test_real_backend_ready_and_graceful_cleanup(tmp_path):
    path, port = config_file(tmp_path)
    identity = str(uuid4())
    child = subprocess.Popen(
        [sys.executable, "-m", "serve", "serve", "--config", str(path), "--workspace", str(tmp_path)],
        env={
            **os.environ,
            "DATAAGENT_INSTANCE_ID": identity,
        },
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        wait_ready(child, f"http://127.0.0.1:{port}/healthz", "test-token", identity, stopped=threading.Event(), timeout=15)
        assert child.poll() is None
        runtime = tmp_path / "home/runtime"
        assert not (tmp_path / ".dataagent/state").exists()
        assert (runtime / "logs").is_dir() and (runtime / "backend.lock").is_file()
        for path in (runtime, runtime / "logs"):
            assert path.stat().st_mode & 0o777 == 0o700
    finally:
        stop_child(child, process_group=True)
    assert child.poll() is not None
    check_port("127.0.0.1", port)
    with workspace_lock(tmp_path / "home/runtime"):
        pass


def _run_serve(tmp_path, *args):
    """Start ``dataagent serve`` against an isolated Home and return when it exits."""
    log_path = tmp_path / "stderr.txt"
    log_file = log_path.open("w")
    env = {**os.environ, "DATAAGENT_HOME": str(tmp_path / "home")}
    for name in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        env.pop(name, None)
    child = subprocess.Popen(
        [sys.executable, "-m", "serve", "serve", *args],
        env=env,
        stdout=subprocess.DEVNULL, stderr=log_file, start_new_session=True,
    )
    try:
        try:
            code = child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            log_file.flush()
            pytest.fail(f"Backend stayed up.\n{log_path.read_text()}")
        log_file.flush()
        return code, log_path.read_text()
    finally:
        if child.poll() is None:
            stop_child(child, process_group=True)
        log_file.close()


def test_missing_config_exits_without_listening(tmp_path):
    code, text = _run_serve(tmp_path, "--config", str(tmp_path / "missing.json"))
    assert code == 1
    assert "dataagent.startup" in text
    assert "Configuration file does not exist" in text
    assert "missing.json" in text


def test_invalid_config_exits_without_listening(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{")
    code, text = _run_serve(tmp_path, "--config", str(path))
    assert code == 1
    assert "dataagent.startup" in text
    assert "Invalid JSON" in text


def test_old_config_exits_with_upgrade_steps(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "schema_version": 3,
        "dataagent": {"model": {"name": "primary"}},
        "a2a": {"agents": []},
        "semantic": {"timeout": 30},
    }))
    code, text = _run_serve(tmp_path, "--config", str(path))
    assert code == 1
    assert "old.json" in text
    assert "schema_version 3 is not supported" in text
    assert "dataagent.model a string" in text
    assert "a2a.agents to a2a.remote_agents" in text
    assert "semantic.timeout to semantic.timeout_seconds" in text


def test_loaded_config_without_model_env_stays_unready(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    for name in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert _prepare(_parser().parse_args(["serve"])) is None
    assert "missing environment variable" in caplog.text


def test_serve_config_comes_only_from_the_flag():
    serve = _parser().parse_args(["serve"])
    a2a = _parser().parse_args(["serve-a2a"])
    chosen = _parser().parse_args(["serve", "--config", "/tmp/chosen.json"])
    assert serve.config is None
    assert a2a.config is None
    assert chosen.config == "/tmp/chosen.json"


def test_backend_entrypoints_share_serve_module():
    from importlib.metadata import distribution
    from pathlib import Path

    from serve.__main__ import main

    package = distribution("datagallery-dataagent")
    assert package.metadata["Name"] == "datagallery-dataagent"
    entrypoint = next(item for item in package.entry_points if item.name == "dataagent")
    assert not any(item.name == "dataagent-v2" for item in package.entry_points)
    assert entrypoint.value == "serve.__main__:main"
    assert entrypoint.load() is main
    assert not (Path(__file__).resolve().parents[1] / "dataagent/cli.py").exists()
    result = subprocess.run(
        [sys.executable, "-m", "serve", "--help"], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert "usage: dataagent" in result.stdout
    assert "DataAgent backend" in result.stdout
    assert "serve-a2a" in result.stdout
    reloaded = subprocess.run(
        [sys.executable, "-m", "serve", "serve", "--help"],
        capture_output=True, text=True, timeout=5,
    )
    assert reloaded.returncode == 0 and "--reload" in reloaded.stdout
    assert not Path(os.environ["DATAAGENT_HOME"]).exists()


def test_degraded_start_keeps_configured_listen_address(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"server": {"host": "0.0.0.0", "port": 8801}}))
    assert _listen(None, str(config)) == ("0.0.0.0", 8801)

    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    assert _listen(None) == ("127.0.0.1", 8790)
    (tmp_path / "broken.json").write_text("{")
    assert _listen(None, str(tmp_path / "broken.json")) == ("127.0.0.1", 8790)


def test_degraded_start_expands_env_in_listen_address(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BIND_HOST", raising=False)
    monkeypatch.delenv("BIND_PORT", raising=False)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"server": {"host": "$env{BIND_HOST}", "port": "$env{BIND_PORT}"}}))
    env_file = tmp_path / "custom.env"
    env_file.write_text("BIND_HOST=0.0.0.0\nBIND_PORT=8802\n")
    assert _listen(None, str(config), str(env_file)) == ("0.0.0.0", 8802)

    monkeypatch.setenv("BIND_HOST", "127.0.0.2")
    monkeypatch.setenv("BIND_PORT", "8803")
    assert _listen(None, str(config)) == ("127.0.0.2", 8803)


def test_missing_env_in_any_section_stays_unready_like_models(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SEMANTIC_PASSWORD", raising=False)
    for name, value in (("LLM_MODEL", "m"), ("LLM_BASE_URL", "http://127.0.0.1:1/v1"), ("LLM_API_KEY", "k")):
        monkeypatch.setenv(name, value)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "models": {"primary": {"name": "$env{LLM_MODEL}", "base_url": "$env{LLM_BASE_URL}",
                               "api_key": "$env{LLM_API_KEY}"}},
        "semantic": {"base_url": "http://127.0.0.1:1", "username": "u", "password": "$env{SEMANTIC_PASSWORD}"},
    }))
    assert _prepare(_parser().parse_args(["serve", "--config", str(config)])) is None
    assert "missing environment variable SEMANTIC_PASSWORD" in caplog.text
