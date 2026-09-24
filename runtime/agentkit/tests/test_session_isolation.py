"""Acceptance checks for native tools and per-session resource organization."""

import json
import shlex
import sys

import pytest
from conftest import ScriptedModel, call
from deepagents.backends import LocalShellBackend
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dataagent.agent import build_agent
from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.bootstrap.paths import ensure_session
from dataagent.extensions.filesystem_backend import build_filesystem_backend


def runtime_for(tmp_path, *workspaces, user_id="default", session_id=None):
    path = tmp_path / f"config-{user_id}.json"
    path.write_text(json.dumps({
        "models": {"primary": {
            "name": "test-model", "base_url": "https://example.invalid/v1", "api_key": "test-secret",
        }},
    }))
    return prepare_runtime(LaunchOptions(
        cwd=tmp_path, workspaces=workspaces, user_id=user_id, session_id=session_id,
        config=path,
    ))


def test_home_is_working_root_without_synthetic_input_workspaces(tmp_path):
    default = runtime_for(tmp_path)
    alice = runtime_for(tmp_path, user_id="alice")
    assert default.paths.home == alice.paths.home == tmp_path / "home"
    assert default.paths.workspaces == alice.paths.workspaces == ()
    assert not (tmp_path / "home/workspaces").exists()
    opened, inputs = default.paths.open_session("empty-inputs", create=True)
    assert inputs == ()
    assert runtime_for(tmp_path, session_id="empty-inputs").paths.workspaces == ()
    assert opened.outputs == default.paths.home / "runtime/users/default/sessions/empty-inputs/outputs"


def test_explicit_workspace_does_not_create_profile_default(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    runtime = runtime_for(tmp_path, source, user_id="alice")
    assert runtime.paths.workspaces[0].path == source
    assert not (runtime.paths.home / "workspaces/alice").exists()


def test_resume_keeps_legacy_default_without_creating_new_one(tmp_path):
    legacy = tmp_path / "home/workspace"
    legacy.mkdir(parents=True)
    original = runtime_for(tmp_path, legacy)
    original.paths.open_session("existing", create=True)
    resumed = runtime_for(tmp_path, session_id="existing")
    assert resumed.paths.workspaces[0].path == legacy
    assert not (resumed.paths.home / "workspaces/default").exists()


def tool_text(result, name):
    return "\n".join(
        str(message.content) for message in result["messages"]
        if isinstance(message, ToolMessage) and message.name == name
    )


async def test_native_file_tools_and_shell_can_access_inputs(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "orders.csv").write_text("orders")
    (right / "customers.csv").write_text("customers")
    runtime = runtime_for(tmp_path, left, right)
    model = ScriptedModel(responses=[
        call("read_file", {"file_path": str(left / "orders.csv")}),
        call("read_file", {"file_path": str(right / "customers.csv")}),
        call("edit_file", {"file_path": str(left / "orders.csv"), "old_string": "orders", "new_string": "changed"}),
        call("execute", {"command": f"cat {shlex.quote(str(right / 'customers.csv'))}"}),
        call("execute", {"command": f"touch {left}/hacked.txt"}),
        AIMessage(content="done"),
    ])
    result = await build_agent(runtime, model=model, session=runtime.paths.for_session("sess-a")).ainvoke({
        "messages": [HumanMessage(content="read both inputs")],
    })
    text = tool_text(result, "read_file")
    assert "orders" in text and "customers" in text
    assert all(message.status == "success" for message in result["messages"]
               if isinstance(message, ToolMessage) and message.name == "read_file")
    assert "customers" in tool_text(result, "execute")
    assert (left / "orders.csv").read_text() == "changed"
    assert (left / "hacked.txt").exists()
    assert all(message.status == "success" for message in result["messages"] if isinstance(message, ToolMessage))


async def test_session_outputs_have_separate_cwd_not_access_isolation(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "orders.csv").write_text("orders")
    runtime = runtime_for(tmp_path, source)
    first = runtime.paths.for_session("sess-a")
    second = runtime.paths.for_session("sess-b")
    model = ScriptedModel(responses=[
        call("write_file", {"file_path": str(first.outputs / "result.csv"), "content": "from-a"}),
        call("execute", {"command": "printf extra >> result.csv"}),
        AIMessage(content="wrote"),
    ])
    await build_agent(runtime, model=model, session=first).ainvoke({
        "messages": [HumanMessage(content="write")],
    })
    assert (first.outputs / "result.csv").read_text() == "from-aextra"
    other = ScriptedModel(responses=[
        call("read_file", {"file_path": str(second.outputs / "result.csv")}),
        call("read_file", {"file_path": str(first.outputs / "result.csv")}),
        call("execute", {"command": "ls"}),
        call("execute", {"command": f"cat {first.outputs / 'result.csv'}"}),
        AIMessage(content="checked"),
    ])
    result = await build_agent(runtime, model=other, session=second).ainvoke({
        "messages": [HumanMessage(content="look")],
    })
    assert "from-a" in tool_text(result, "read_file")
    assert "from-a" in tool_text(result, "execute")
    assert not (second.outputs / "result.csv").exists()


@pytest.mark.parametrize("child", [False, True])
async def test_file_tools_and_shell_share_real_paths(tmp_path, child, common_plugin):
    root = tmp_path / "中文 space {data}[1]"
    root.mkdir()
    (tmp_path / "home/config.json").write_text(json.dumps({"plugins": {"enabled": ["common"]}}))
    runtime = runtime_for(tmp_path, root)
    session = runtime.paths.for_session("same-path")
    output = session.outputs / "自我介绍 [1].md"
    command = f"wc -m {shlex.quote(str(output))} && printf shell >> {shlex.quote(str(output))}"
    responses = [
        call("write_file", {"file_path": str(output), "content": "hello"}),
        call("execute", {"command": command}),
        call("read_file", {"file_path": str(output)}),
        AIMessage(content="verified"),
    ]
    if child:
        responses = [call("task", {"subagent_type": "general-purpose", "description": "Write and verify a file"}),
                     *responses, AIMessage(content="done")]
    model = ScriptedModel(responses=responses)
    await build_agent(runtime, model=model, session=session).ainvoke({
        "messages": [HumanMessage(content="Write and verify a file")],
    })
    assert output.read_text() == "helloshell"
    messages = [message for request in model.requests for message in request if isinstance(message, ToolMessage)]
    assert any(message.name == "read_file" and "helloshell" in str(message.content) for message in messages)
    assert any(message.name == "execute" and "5" in str(message.content) for message in messages)
    assert all(message.artifact["exit_code"] == 0 for message in messages if message.name == "execute")
    for request in model.requests:
        prompt = "\n".join(str(message.content) for message in request if message.type == "system")
        assert str(session.outputs) in prompt
        assert str(root) in prompt
        assert "/workspace/<n>" not in prompt and "under /output" not in prompt


def test_backend_uses_native_file_operations_without_access_filters(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    (source / "input.txt").write_text("input-evidence")
    runtime = runtime_for(tmp_path, source)
    session = runtime.paths.for_session("one")
    backend = build_filesystem_backend(runtime, session)
    other = runtime.paths.for_session("two")
    ensure_session(runtime.paths.home, other)
    evidence = other.outputs / "evidence.txt"
    evidence.write_text("other-session-evidence")
    assert type(backend.default) is LocalShellBackend
    assert backend.routes == {}
    assert backend.default.virtual_mode is False
    assert backend.default.cwd == session.outputs
    # Regression: listing the session parent no longer raises Read access denied.
    assert backend.ls(str(session.root)).error is None
    assert backend.write("relative.txt", "relative-evidence").error is None
    assert (session.outputs / "relative.txt").read_text() == "relative-evidence"
    assert any(item["path"] == str(session.outputs / "relative.txt") for item in backend.glob("*.txt").matches)
    assert backend.read(str(evidence)).error is None
    assert backend.read(str(tmp_path / "config-default.json")).error is None
    assert backend.edit(str(source / "input.txt"), "input", "changed").error is None
    assert (source / "input.txt").read_text() == "changed-evidence"
    uploaded = str(source / "uploaded.txt")
    assert backend.upload_files([(uploaded, b"uploaded")])[0].error is None
    assert backend.download_files([uploaded])[0].content == b"uploaded"
    assert any(item["path"] == uploaded for item in backend.ls(str(source)).entries)
    assert any(item["path"] == uploaded for item in backend.glob("*.txt", str(source)).matches)
    assert backend.grep("uploaded", str(source)).matches
    assert backend.delete(uploaded).error is None
    assert not (source / "uploaded.txt").exists()



def test_profiles_do_not_share_session_directories(tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    first = runtime_for(tmp_path, source, user_id="alpha")
    opened, workspaces = first.paths.open_session("sess-a", create=True)
    (opened.outputs / "result.csv").write_text("alpha")
    second = runtime_for(tmp_path, source, user_id="beta")
    assert second.paths.for_session("sess-a").root != opened.root
    assert not second.paths.for_session("sess-a").outputs.exists()
    assert workspaces == first.paths.workspaces


def test_resume_keeps_the_saved_workspace_binding(tmp_path):
    source, other = tmp_path / "input", tmp_path / "other"
    source.mkdir()
    other.mkdir()
    runtime = runtime_for(tmp_path, source)
    runtime.paths.open_session("sess-a", create=True)
    resumed = runtime_for(tmp_path, user_id="default", session_id="sess-a")
    assert resumed.paths.workspaces == runtime.paths.workspaces
    with pytest.raises(ValueError, match="different workspace list"):
        runtime_for(tmp_path, other, session_id="sess-a")
    with pytest.raises(ValueError, match="not in profile"):
        runtime_for(tmp_path, source, session_id="missing")
    with pytest.raises(ValueError, match="not in profile"):
        runtime_for(tmp_path, source, user_id="beta", session_id="sess-a")


def test_native_shell_can_cd_to_absolute_output_and_share_file_paths(tmp_path, monkeypatch):
    source = tmp_path / "中文 input space"
    source.mkdir()
    (source / "input.txt").write_text("input-evidence")
    monkeypatch.setenv("DATAAGENT_SHELL_TEST", "inherited")
    runtime = runtime_for(tmp_path, source)
    session = runtime.paths.for_session("native-shell")
    backend = build_filesystem_backend(runtime, session)
    assert type(backend.default).execute is LocalShellBackend.execute
    assert backend.execute("pwd").output.strip() == str(session.outputs)
    assert backend.write(str(session.outputs / "result.txt"), "file-evidence").error is None
    result = backend.execute(
        f"cd {shlex.quote(str(session.outputs))} && "
        f"cat {shlex.quote(str(source / 'input.txt'))} && "
        'cat result.txt && printf "%s" "$DATAAGENT_SHELL_TEST" >> result.txt',
    )
    assert result.exit_code == 0
    assert "input-evidence" in result.output and "file-evidence" in result.output
    assert "file-evidenceinherited" in str(backend.read(str(session.outputs / "result.txt")))
    assert backend.ls(str(session.root)).error is None


def test_native_shell_preserves_error_timeout_and_truncation_results(tmp_path):
    runtime = runtime_for(tmp_path)
    session = runtime.paths.for_session("shell-results")
    backend = build_filesystem_backend(runtime, session)
    result = backend.execute("printf problem >&2; exit 7")
    assert result.exit_code == 7
    assert "[stderr] problem" in result.output
    assert not result.truncated
    assert backend.execute("").exit_code == 1
    python = shlex.quote(sys.executable)
    result = backend.execute(f'{python} -c "import time; time.sleep(0.2)"', timeout=0.01)
    assert result.exit_code == 124 and "timed out" in result.output
    assert backend.default._default_timeout == runtime.timeout_seconds
    result = backend.execute(f'{python} -c "print(\'x\' * 100001)"')
    assert result.exit_code == 0 and result.truncated
