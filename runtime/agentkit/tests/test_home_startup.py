"""Home-only startup: cwd extensions are ignored and owned processes exit cleanly."""

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
from uuid import uuid4

import pytest
from process_helpers import stop_child
from test_launcher import check_port, config_file
from test_product_e2e import ANSI, REPOSITORY, Terminal


def ignored_cwd(tmp_path):
    cwd = tmp_path / "中文 launch directory"
    extensions = cwd / ".dataagent"
    extensions.mkdir(parents=True)
    (extensions / "config.json").write_text("not json")
    marker = tmp_path / "unexpected-import"
    hook = extensions / "hooks/observe.py"
    hook.parent.mkdir()
    hook.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    return cwd, marker


def test_backend_ready_without_project_handshake_and_eof_cleanup(tmp_path):
    path, port = config_file(tmp_path)
    cwd, marker = ignored_cwd(tmp_path)
    identity = str(uuid4())
    child = subprocess.Popen([
        sys.executable, "-m", "restapi", "serve", "--stdio-ready", "--config", str(path),
    ], cwd=cwd, env={**os.environ, "DATAAGENT_V2_INSTANCE_ID": identity},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    try:
        assert select.select([child.stdout], [], [], 15)[0], "No startup message"
        ready = json.loads(child.stdout.readline())
        assert ready["type"] == "ready" and ready["instanceId"] == identity
        assert not marker.exists()
        child.stdin.close()
        assert child.wait(timeout=10) == 0
        check_port("127.0.0.1", port)
    finally:
        stop_child(child, process_group=True)
        for pipe in (child.stdin, child.stdout, child.stderr):
            pipe.close()


@pytest.mark.parametrize("columns", [60, 80])
def test_tui_starts_without_project_prompt_and_restores_terminal(tmp_path, columns):
    path, port = config_file(tmp_path)
    cwd, marker = ignored_cwd(tmp_path)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, columns, 0, 0))
    process = subprocess.Popen([
        "node", str(REPOSITORY / "apps/tui/dist/index.js"), "--v2", "--config", str(path),
    ], cwd=cwd, env={**os.environ, "INIT_CWD": str(cwd), "NO_COLOR": "1", "TERM": "xterm-256color"},
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    terminal = Terminal(process, master)
    try:
        terminal.read_until("Summarize 1, 2, 3", timeout=25)
        terminal.wait_for_input()
        text = ANSI.sub("", terminal.output.decode("utf-8", errors="replace"))
        assert "Project extensions" not in text and "Select an option" not in text
        assert not marker.exists()
        terminal.submit("/exit")
        terminal.wait_for_exit()
        assert termios.tcgetattr(master)[3] & termios.ICANON
        check_port("127.0.0.1", port)
    finally:
        (tmp_path / "terminal.log").write_text(
            ANSI.sub("", terminal.output.decode("utf-8", errors="replace")))
        stop_child(process, process_group=True)
        os.close(master)
