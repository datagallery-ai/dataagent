import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from uuid import uuid4

import pytest
from process_helpers import stop_child, wait_ready

from serve.__main__ import check_port
from serve.sessions import workspace_lock


def open_listener():
    """Bind an ephemeral port and keep listening until the child inherits the socket."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def spawn_inherited(argv, listener, *, env, fd_env="DATAAGENT_LISTEN_FD", **kwargs):
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
    listener = open_listener()
    path, port = config_file(tmp_path, listener)
    identity = str(uuid4())
    child = spawn_inherited(
        [sys.executable, "-m", "serve", "serve", "--config", str(path), "--workspace", str(tmp_path)],
        listener,
        env={
            **os.environ,
            "DATAAGENT_V2_INSTANCE_ID": identity,
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


def test_missing_config_stays_up_without_an_engine(tmp_path):
    port = 8790
    log_path = tmp_path / "stderr.txt"
    log_file = log_path.open("w")
    child = subprocess.Popen(
        [sys.executable, "-m", "serve", "serve", "--config", str(tmp_path / "missing.json")],
        env=os.environ,
        stdout=subprocess.DEVNULL, stderr=log_file, start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        body = None
        while time.monotonic() < deadline:
            if child.poll() is not None:
                log_file.flush()
                pytest.fail(f"Backend exited {child.returncode}.\n{log_path.read_text()}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
                    body = json.load(response)
                break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(0.1)
        else:
            log_file.flush()
            pytest.fail(f"healthz was not served.\n{log_path.read_text()}")
        assert body["success"] is True
        assert body["data"]["conversation"]["ready"] is False
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=2)
            pytest.fail("/ready succeeded while the engine was down")
        except urllib.error.HTTPError as error:
            assert error.code == 503
    finally:
        stop_child(child, process_group=True)
        log_file.close()
    assert "DataAgent:" in log_path.read_text()


def test_backend_entrypoints_share_serve_module():
    from importlib.metadata import distribution
    from pathlib import Path

    from serve.__main__ import main

    package = distribution("dataagent")
    assert package.metadata["Name"] == "dataagent"
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
