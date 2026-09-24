"""Opt-in real-provider acceptance through the public CLI and existing Ink TUI.

DATAAGENT_V2_LIVE_ENV=/absolute/path/to/.env uv run --project runtime/agentkit \
    pytest runtime/agentkit/tests/test_product_e2e.py -q

Only synthetic prompts are sent. Credentials remain in the child environment;
the test config, temporary workspace and diagnostic assertions contain no keys.
"""

import asyncio
import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import socket
import sqlite3
import struct
import subprocess
import termios
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from process_helpers import stop_child

from dataagent.agent import build_agent
from restapi.__main__ import check_port

PROJECT = Path(__file__).resolve().parents[1]
REPOSITORY = PROJECT.parents[1]
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


class Terminal:
    def __init__(self, process, descriptor):
        self.process, self.descriptor = process, descriptor
        self.output = b""

    def read_until(self, text, timeout=190, ready=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in ANSI.sub("", self.output.decode("utf-8", errors="replace")) and (ready is None or ready()):
                self.settle()
                return
            if text == "Run completed" and "Error [" in ANSI.sub("", self.output.decode("utf-8", errors="replace")):
                pytest.fail("The model run failed; inspect the temporary workspace logs")
            assert self.process.poll() is None, "Product exited before the expected terminal state"
            if select.select([self.descriptor], [], [], 0.1)[0]:
                self.output += os.read(self.descriptor, 65536)
        pytest.fail(f"Terminal did not reach {text!r}; inspect the temporary workspace logs")

    def wait_for_round(self, workspace, previous_run_id=None):
        def finished():
            row = saved_session(workspace)
            return row is not None and row["last_run_id"] != previous_run_id and row["status"] == "complete"

        # Ink redraws historical completion labels on resize and while composing.
        # A label alone must not terminate the still-running next round.
        self.read_until("Run completed", ready=finished)

    def submit(self, text):
        assert not (termios.tcgetattr(self.descriptor)[3] & termios.ICANON), "TUI failed to enable raw terminal input"
        self.output = b""
        os.write(self.descriptor, text.encode("utf-8"))
        self.settle()  # Drain rendering before a separate physical Enter event.
        os.write(self.descriptor, b"\r")

    def settle(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not select.select([self.descriptor], [], [], 0.1)[0]:
                return
            try:
                chunk = os.read(self.descriptor, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                return
            if not chunk:
                return
            self.output += chunk

    def wait_for_input(self):
        # macOS TTY writes are synchronous. Keep draining the first frame while
        # Ink installs effects; sleeping here can block it before raw-mode setup.
        deadline = time.monotonic() + 5
        while termios.tcgetattr(self.descriptor)[3] & termios.ICANON:
            assert time.monotonic() < deadline, "TUI failed to initialize terminal input"
            if select.select([self.descriptor], [], [], 0.05)[0]:
                self.output += os.read(self.descriptor, 65536)

    def resize(self, columns, rows):
        fcntl.ioctl(self.descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        os.killpg(self.process.pid, signal.SIGWINCH)

    def wait_for_exit(self, expected_code=0):
        deadline = time.monotonic() + 10
        while self.process.poll() is None:
            assert time.monotonic() < deadline, "Product did not shut down after /exit"
            if select.select([self.descriptor], [], [], 0.05)[0]:
                try:
                    self.output += os.read(self.descriptor, 65536)
                except OSError:
                    break  # PTY reports EIO when its last slave closes.
        assert self.process.wait(timeout=5) == expected_code


@contextmanager
def product(config, environment, resume=None, env_file=None):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
    arguments = ["npm", "run", "start:tui", "--", "--v2", "--config", os.path.relpath(config, REPOSITORY),
                 "--workspace", str(config.parent)]
    if env_file:
        arguments += ["--env-file", os.path.relpath(env_file, REPOSITORY)]
    if resume:
        arguments += ["--resume", resume]
    process = subprocess.Popen(
        arguments, cwd=REPOSITORY, env={**environment, "TERM": "xterm-256color"},
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
    )
    os.close(slave)
    terminal = Terminal(process, master)
    try:
        terminal.read_until("Restored:" if resume else "Summarize 1, 2, 3", timeout=40)
        terminal.wait_for_input()
        yield terminal
        if process.poll() is None:
            terminal.submit("/exit")
            terminal.wait_for_exit()
    finally:
        trace = ANSI.sub("", terminal.output.decode("utf-8", errors="replace"))
        if environment.get("LLM_API_KEY"):
            trace = trace.replace(environment["LLM_API_KEY"], "[REDACTED]")
        (config.parent / "terminal-test.log").write_text(trace)
        stop_child(process, process_group=True)
        os.close(master)


def saved_session(workspace):
    del workspace
    with sqlite3.connect(Path(os.environ["DATAAGENT_HOME"]) / "runtime/sessions.sqlite") as database:
        database.row_factory = sqlite3.Row
        row = database.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def test_terminal_wait_ignores_previous_round_completion(monkeypatch):
    from types import SimpleNamespace

    rows = iter([
        {"last_run_id": "previous", "status": "complete"},
        {"last_run_id": "current", "status": "running"},
        {"last_run_id": "current", "status": "complete"},
    ])
    observed = []

    def session(_):
        row = next(rows)
        observed.append(row)
        return row

    monkeypatch.setattr(__import__(__name__), "saved_session", session)
    monkeypatch.setattr(select, "select", lambda *_: ([], [], []))
    terminal = Terminal(SimpleNamespace(poll=lambda: None), -1)
    terminal.output = "Run completed · 8.5s".encode()
    terminal.wait_for_round(Path("unused"), "previous")
    assert len(observed) == 3


def test_cli_accepts_plain_enter_without_a_model_call(tmp_path):
    from test_launcher import config_file

    config, port = config_file(tmp_path)
    environment = {**os.environ, "LLM_API_KEY": "fake-test-key"}
    with product(config, environment) as terminal:
        terminal.submit("/help")
        terminal.read_until("/clear (new conversation)", timeout=5)
    check_port("127.0.0.1", port)


def test_tui_resolves_explicit_env_file_from_npm_launch_directory(tmp_path):
    from test_launcher import config_file

    config, port = config_file(tmp_path)
    config_data = json.loads((PROJECT / "config.example.json").read_text())
    config_data["server"]["port"] = port
    config.write_text(json.dumps(config_data))
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        f"LLM_MODEL=env-file-model\nLLM_BASE_URL=https://example.invalid/v1\n"
        f"LLM_API_KEY=fake-test-key\nDATAAGENT_WORKSPACE={tmp_path}\n"
    )
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "DATAAGENT_WORKSPACE"}}
    with product(config, environment, env_file=env_file) as terminal:
        terminal.read_until("env-file-model", timeout=5)
        terminal.submit("/help")
        terminal.read_until("/clear (new conversation)", timeout=5)
    check_port("127.0.0.1", port)


def test_tui_reports_occupied_port_without_reusing_or_stopping_service(tmp_path):
    from test_launcher import config_file

    config, port = config_file(tmp_path)
    with socket.socket() as service:
        service.bind(("127.0.0.1", port))
        service.listen()
        result = subprocess.run([
            "node", str(REPOSITORY / "apps/tui/dist/index.js"), "--v2", "--config", str(config), "--workspace", str(tmp_path),
        ], capture_output=True, text=True, timeout=15)
        assert result.returncode == 1
        assert "occupied; no existing process was changed" in result.stderr
        assert service.fileno() >= 0
        assert not (tmp_path / ".dataagent/state").exists()


def test_tui_backend_cleanup_when_node_parent_is_killed(tmp_path):
    from test_launcher import config_file

    config, port = config_file(tmp_path)
    source = f"""
import {{startV2Backend}} from {json.dumps((REPOSITORY / 'apps/tui/dist/v2-backend.js').as_uri())};
const backend = await startV2Backend({{configPath: process.argv[1], workspace: process.argv[2]}});
console.log('BACKEND_READY');
await new Promise(() => {{}});
"""
    parent = subprocess.Popen(
        ["node", "--input-type=module", "-e", source, str(config), str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        assert select.select([parent.stdout], [], [], 15)[0], "TUI did not start its backend"
        assert parent.stdout.readline().strip() == "BACKEND_READY"
        parent.kill()  # No JS finally/exit handler can run; backend must detect pipe EOF.
        parent.wait(timeout=5)
        deadline = time.monotonic() + 10
        while True:
            try:
                check_port("127.0.0.1", port)
                break
            except RuntimeError:
                assert time.monotonic() < deadline, "Backend survived the Node TUI's death"
                time.sleep(0.1)
    finally:
        stop_child(parent, process_group=True)
        parent.stdout.close()
        parent.stderr.close()


def test_tui_restores_terminal_when_its_backend_exits(tmp_path):
    from test_launcher import config_file

    config, port = config_file(tmp_path)
    with product(config, {**os.environ, "LLM_API_KEY": "fake-test-key"}) as terminal:
        log = (Path(os.environ["DATAAGENT_HOME"]) / "runtime/logs/backend.log").read_text()
        backend_pid = int(re.search(r"Started server process \[(\d+)\]", log)[1])
        command = subprocess.check_output(["ps", "-p", str(backend_pid), "-o", "command="], text=True)
        assert "serve --stdio-ready" in command and str(config) in command
        os.kill(backend_pid, signal.SIGTERM)
        terminal.read_until("V2 backend exited", timeout=10)
        terminal.wait_for_exit(expected_code=1)
        assert termios.tcgetattr(terminal.descriptor)[3] & termios.ICANON
    check_port("127.0.0.1", port)


def test_cli_suspend_restores_terminal_and_resumes_input(tmp_path):
    from test_launcher import config_file

    config, port = config_file(tmp_path)
    with product(config, {**os.environ, "LLM_API_KEY": "fake-test-key"}) as terminal:
        try:
            os.write(terminal.descriptor, b"\x1a")
            deadline = time.monotonic() + 5
            while not (termios.tcgetattr(terminal.descriptor)[3] & termios.ICANON):
                assert time.monotonic() < deadline, "Suspend did not restore cooked terminal input"
                terminal.settle()
            state = subprocess.check_output(["ps", "-p", str(terminal.process.pid), "-o", "stat="], text=True)
            assert "T" in state, "The owned foreground job was not suspended"
        finally:
            os.killpg(terminal.process.pid, signal.SIGCONT)
        terminal.wait_for_input()
        terminal.settle()
        terminal.submit("/help")
        terminal.read_until("/clear (new conversation)", timeout=5)
    check_port("127.0.0.1", port)


async def saved_messages(workspace, row):
    home = Path(os.environ["DATAAGENT_HOME"])
    async with AsyncSqliteSaver.from_conn_string(str(home / "runtime/checkpoints.sqlite")) as saver:
        # Deep Agents uses DeltaChannels: let the native graph reconstruct them,
        # rather than assuming every raw SQLite checkpoint contains all messages.
        from conftest import ScriptedModel
        from langchain_core.messages import AIMessage

        from dataagent.bootstrap import ExtensionLocations, Runtime, RuntimePaths, StartupReport
        from dataagent.bootstrap.paths import WorkspaceInput
        from dataagent.settings import Settings

        settings = Settings.model_validate({"models": {
            "primary": {"name": "checkpoint-reader", "base_url": "https://example.invalid/v1", "api_key": "unused"},
        }})
        paths = RuntimePaths.resolve(home, (WorkspaceInput("workspace-0", Path(workspace)),))
        builtin = tuple(sorted(child for child in paths.builtin_plugins.iterdir() if child.is_dir()))
        runtime = Runtime(settings, paths,
                          StartupReport((), ()),
                          ExtensionLocations(plugin_roots=builtin))
        graph = build_agent(runtime, checkpointer=saver, model=ScriptedModel(responses=[AIMessage(content="unused")]))
        state = await graph.aget_state({"configurable": {
            "thread_id": row["thread_id"], "checkpoint_id": row["last_checkpoint_id"],
        }})
        return state.values["messages"]


def exercise_query_and_resume(tmp_path, values):
    required = ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY")
    assert all(values.get(key) for key in required), "Live environment is missing model settings"
    environment = {**os.environ, **{key: values[key] for key in required}, "DATAAGENT_WORKSPACE": str(tmp_path)}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "config.json"
    config_data = json.loads((PROJECT / "config.example.json").read_text())
    config_data["plugins"] = {"enabled": ["common"], "paths": []}
    config_data["server"]["port"] = port
    config.write_text(json.dumps(config_data))

    with product(config, environment) as terminal:
        terminal.submit("Use common__summarize_numbers to summarize 2, 4, 6. Reply briefly in English.")
        terminal.wait_for_round(tmp_path)
        first = saved_session(tmp_path)
        assert first["status"] == "complete"
        messages = asyncio.run(saved_messages(tmp_path, first))
        statistics = [json.loads(message.content) for message in messages
                      if isinstance(message, ToolMessage) and message.name == "common__summarize_numbers"]
        assert statistics and statistics[-1]["mean"] == 4
        audit_path = Path(os.environ["DATAAGENT_HOME"]) / "runtime/logs/backend.log"
        previous_audit = audit_path.read_text()
        terminal.resize(70, 26)
        terminal.submit(
            "Delegate statistics for 1, 2, 3 to general-purpose using task. "
            "Ask it to first read the number-summary Skill and then call its statistics tool. "
            "Return the results briefly in English."
        )
        terminal.wait_for_round(tmp_path, first["last_run_id"])
        row = saved_session(tmp_path)
        assert row["status"] == "complete"
        audit = audit_path.read_text()
        assert audit.startswith(previous_audit)
        records = []
        for line in audit[len(previous_audit):].splitlines():
            event = next((item for item in line.split() if item in {
                "hook.before_agent", "hook.after_agent", "hook.before_tool", "hook.after_tool",
            }), None)
            if event:
                records.append({"event": event, **dict(item.split("=", 1) for item in line.split() if "=" in item)})
        assert {record["agent"] for record in records if record["event"] == "hook.before_agent"} == {
            "dataagent-v2", "general-purpose",
        }
        children = [record for record in records if record["event"] == "hook.before_tool"
                    and record.get("agent") == "general-purpose"]
        skill_read = next(record for record in children if record["tool"] == "read_file")
        calculation = next(record for record in children if record["tool"] == "common__summarize_numbers")
        ends = {record["call"]: index for index, record in enumerate(records) if record["event"] == "hook.after_tool"}
        assert skill_read["call"] in ends and calculation["call"] in ends
        assert ends[skill_read["call"]] < records.index(calculation), "The specialist must load the Skill before its tool"
        assert any(record["event"] == "hook.after_agent" and record["agent"] == "general-purpose"
                   for record in records)
        task = next(record for record in records if record["event"] == "hook.before_tool" and record.get("tool") == "task")
        assert task["call"] in ends
    check_port("127.0.0.1", port)

    with product(config, environment, row["thread_id"]) as terminal:
        terminal.submit("Which numbers did I ask the specialist to summarize? Reply with the numbers only; no tools.")
        terminal.wait_for_round(tmp_path, row["last_run_id"])
        final = saved_session(tmp_path)
        assert final["thread_id"] == row["thread_id"] and final["status"] == "complete"
        messages = asyncio.run(saved_messages(tmp_path, final))
        assert sum(message.type == "human" for message in messages) == 3
        assert all(number in messages[-1].content for number in ("1", "2", "3"))
    check_port("127.0.0.1", port)
    log_dir = Path(os.environ["DATAAGENT_HOME"]) / "runtime/logs"
    assert (log_dir / "tui.log").exists()
    secret = values["LLM_API_KEY"]
    for logfile in log_dir.glob("*.log"):
        assert secret not in logfile.read_text(), "Credential appeared in a product log"


@pytest.mark.live
def test_real_cli_tool_delegation_and_restart_resume(tmp_path, common_plugin):
    source = os.environ.get("DATAAGENT_V2_LIVE_ENV")
    if not source:
        pytest.skip("Set DATAAGENT_V2_LIVE_ENV to explicitly opt into provider calls")
    exercise_query_and_resume(tmp_path, {**dotenv_values(source), **os.environ})


def test_full_product_with_deterministic_chat_completions_provider(tmp_path, common_plugin):
    """Only the remote LLM is scripted; every product process/tool/checkpoint is real."""
    requests = []
    skill = common_plugin / "skills/number-summary/SKILL.md"

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == "/v1/chat/completions"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            messages = body["messages"]
            system = str(messages[0]["content"])
            last = messages[-1]
            function = arguments = None
            if "Complete the delegated task using the enabled tools and skills." in system:
                if last["role"] == "user":
                    function, arguments = "read_file", {"file_path": str(skill)}
                elif messages[-2]["tool_calls"][0]["function"]["name"] == "read_file":
                    function, arguments = "common__summarize_numbers", {"numbers": [1, 2, 3]}
                else:
                    content = "Count 3, sum 6, mean 2, min 1, max 3."
            elif last["role"] == "tool":
                content = "Statistics computed: " + last["content"]
            elif "Which numbers" in last["content"]:
                content = "1, 2, 3"
            elif "Delegate statistics" in last["content"]:
                function, arguments = "task", {"subagent_type": "general-purpose", "description": "Summarize 1, 2, 3 using the Skill and tool."}
            else:
                function, arguments = "common__summarize_numbers", {"numbers": [2, 4, 6]}
            if function:
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": str(uuid4()), "type": "function",
                    "function": {"name": function, "arguments": json.dumps(arguments)},
                }]}
            else:
                delta = {"role": "assistant", "content": content}
            identifier = "chatcmpl-" + str(uuid4())
            chunks = [{"id": identifier, "object": "chat.completion.chunk", "created": 1,
                       "model": "deterministic-provider", "choices": [{"index": 0, "delta": item,
                       "finish_reason": finish}]} for item, finish in [
                           (delta, None), ({}, "tool_calls" if function else "stop"),
                       ]]
            data = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            exercise_query_and_resume(tmp_path, {
                "LLM_MODEL": "deterministic-provider", "LLM_API_KEY": "fake-test-key",
                "LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            })
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert len(requests) == 8


@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGHUP])
def test_tui_signal_shutdown_restores_terminal_and_stops_owned_backend(tmp_path, shutdown_signal):
    from test_launcher import config_file

    config, port = config_file(tmp_path)
    with product(config, {**os.environ, "LLM_API_KEY": "fake-test-key"}) as terminal:
        processes = subprocess.check_output(["ps", "-Ao", "pid=,ppid=,command="], text=True)
        entries = [line.strip().split(maxsplit=2) for line in processes.splitlines()]
        parents = {int(pid): int(parent) for pid, parent, _ in entries}
        def owned(pid):
            while pid in parents and pid != terminal.process.pid:
                pid = parents[pid]
            return pid == terminal.process.pid
        node_pid = next(int(pid) for pid, _, command in entries
                        if "node -- dist/index.js --v2" in command and owned(int(pid)))
        os.kill(node_pid, shutdown_signal)
        terminal.wait_for_exit(expected_code=130)
        assert termios.tcgetattr(terminal.descriptor)[3] & termios.ICANON
        assert b"\x1b[?1049l" in terminal.output
        assert b"\x1b[?25h" in terminal.output
    check_port("127.0.0.1", port)
