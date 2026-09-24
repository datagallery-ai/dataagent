import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest
from process_helpers import stop_child, wait_ready

from restapi.__main__ import check_port
from restapi.sessions import workspace_lock


def config_file(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
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
    path, port = config_file(tmp_path)
    identity = str(uuid4())
    child = subprocess.Popen(
        [sys.executable, "-m", "restapi", "serve", "--config", str(path), "--workspace", str(tmp_path)],
        env={**os.environ, "DATAAGENT_V2_TOKEN": "test-token", "DATAAGENT_V2_INSTANCE_ID": identity},
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


def test_real_backend_exits_when_launcher_parent_dies(tmp_path):
    path, port = config_file(tmp_path)
    identity = str(uuid4())
    parent_script = """
import os, subprocess, sys
child = subprocess.Popen(
    [sys.executable, '-m', 'restapi', 'serve', '--stdio-ready', '--config', sys.argv[1], '--workspace', sys.argv[2]],
    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True,
)
print(child.pid, flush=True)
child.wait()
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_script, str(path), str(tmp_path)], stdout=subprocess.PIPE, text=True,
        env={**os.environ, "DATAAGENT_V2_TOKEN": "test-token", "DATAAGENT_V2_INSTANCE_ID": identity},
        start_new_session=True,
    )
    child_pid = int(parent.stdout.readline().strip())
    original_identity = subprocess.check_output(["ps", "-p", str(child_pid), "-o", "lstart=,command="], text=True)
    try:
        wait_ready(parent, f"http://127.0.0.1:{port}/healthz", "test-token", identity, stopped=threading.Event(), timeout=15)
        stop_child(parent, process_group=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = subprocess.run(["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True)
            if result.returncode or result.stdout.strip().startswith("Z"):
                break
            time.sleep(0.1)
        else:
            pytest.fail("Backend survived the launcher's death")
        check_port("127.0.0.1", port)
    finally:
        stop_child(parent, process_group=True)
        current = subprocess.run(["ps", "-p", str(child_pid), "-o", "lstart=,command="], capture_output=True, text=True)
        if current.returncode == 0 and current.stdout == original_identity:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        parent.stdout.close()


def test_missing_config_fails_without_backend(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "restapi", "serve", "--config", str(tmp_path / "missing.json")],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 1
    assert "DataAgent:" in result.stderr
    assert not (tmp_path / ".dataagent/state").exists()


def test_backend_entrypoints_share_restapi_module():
    from importlib.metadata import distribution
    from pathlib import Path

    from restapi.__main__ import main

    package = distribution("dataagent")
    assert package.metadata["Name"] == "dataagent"
    entrypoint = next(item for item in package.entry_points if item.name == "dataagent")
    assert not any(item.name == "dataagent-v2" for item in package.entry_points)
    assert entrypoint.value == "restapi.__main__:main"
    assert entrypoint.load() is main
    assert not (Path(__file__).resolve().parents[1] / "dataagent/cli.py").exists()
    result = subprocess.run(
        [sys.executable, "-m", "restapi", "--help"], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0
    assert "usage: dataagent" in result.stdout
    assert "DataAgent backend" in result.stdout
    assert not Path(os.environ["DATAAGENT_HOME"]).exists()
