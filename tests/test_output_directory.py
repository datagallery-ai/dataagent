"""Configured output roots and durable session bindings, independent of the host UI."""

import json
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ScriptedModel, call
from langchain_core.messages import AIMessage

from dataagent import LaunchOptions, create_agent, prepare_runtime
from dataagent.bootstrap.paths import (
    SessionPaths,
    WorkspaceInput,
    find_session_binding,
    read_session_binding,
)
from dataagent.lowlevel import build_graph


@pytest.fixture
def binding_sql(monkeypatch):
    """Capture actual SQLite statements, including registration inside graph assembly."""
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    return statements


def configured(runtime, value, filename="outputs.yaml"):
    path = runtime.paths.home / filename
    path.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "test-model",
                        "base_url": "https://example.invalid/v1",
                        "api_key": "test",
                    }
                },
                "dataagent": {"output_root": value},
            }
        )
    )
    return prepare_runtime(LaunchOptions(config=path))


@pytest.mark.parametrize("value", [None, "/tmp/dataagent-config-test/reports", "~/reports"])
def test_output_config_resolves_paths_without_creating_directories(runtime, value):
    prepared = configured(runtime, value)
    expected = (
        runtime.paths.home / "outputs"
        if value is None
        else Path.home() / "reports"
        if value.startswith("~")
        else Path(value).resolve()
    )
    assert prepared.paths.output_root == expected
    session = prepared.paths.for_session("new-session")
    output = expected / "default/new-session"
    assert session.outputs == output
    assert find_session_binding(session.root) is None
    assert session.traces == session.root / "traces"
    assert session.artifacts == output / "artifacts"


def test_output_root_uses_config_layer_precedence(runtime):
    configured(runtime, str(runtime.paths.home / "default-reports"), "config.yaml")
    overriding = configured(runtime, str(runtime.paths.home / "custom-reports"))
    assert overriding.paths.output_root == runtime.paths.home / "custom-reports"
    cleared = configured(runtime, None)
    assert cleared.paths.output_root == runtime.paths.home / "outputs"


@pytest.mark.parametrize("existing", [False, True])
def test_default_outputs_keep_private_parents_without_changing_business_permissions(
    runtime, existing
):
    paths = replace(runtime.paths, user_id="alice")
    assert paths.output_root == paths.home / "outputs"
    assert not paths.output_root.exists()
    session = paths.for_session("task")
    assert session.outputs == paths.home / "outputs/alice/task"
    assert not session.root.exists()
    paths.home.chmod(0o755)
    if existing:
        session.artifacts.mkdir(parents=True)
        for path in (paths.output_root, session.outputs.parent, session.outputs, session.artifacts):
            path.chmod(0o775)
    previous_umask = os.umask(0o022)
    try:
        opened, _ = paths.open_session("task", create=True)
    finally:
        os.umask(previous_umask)
    assert opened == session
    for path in (paths.output_root, session.outputs.parent, session.root, session.traces):
        assert path.stat().st_mode & 0o777 == 0o700
    for path in (session.outputs, session.artifacts):
        assert path.stat().st_mode & 0o777 == (0o775 if existing else 0o755)
    assert paths.home.stat().st_mode & 0o777 == 0o755
    assert {item.name for item in session.root.iterdir()} == {"traces"}
    assert {item.name for item in session.outputs.iterdir()} == {"artifacts"}


@pytest.mark.parametrize("custom_root", [False, True])
def test_new_sessions_are_separate_and_existing_sessions_keep_their_outputs(
    runtime, tmp_path, custom_root
):
    root = tmp_path / "reports" if custom_root else runtime.paths.home / "outputs"
    original = configured(runtime, str(root) if custom_root else None)
    first, _ = original.paths.open_session("one", create=True)
    second, _ = original.paths.open_session("two", create=True)
    other, _ = replace(original.paths, user_id="alice").open_session("one", create=True)
    assert first.outputs == root / "default/one"
    assert len({first.outputs, second.outputs, other.outputs}) == 3
    (first.outputs / "report.csv").write_text("saved")
    assert read_session_binding(first).outputs == first.outputs
    assert {item.name for item in first.root.iterdir()} == {"traces"}
    assert first.traces.is_relative_to(runtime.paths.home)
    changed = configured(runtime, str(tmp_path / "different-reports"))
    assert changed.paths.for_session("one") == first
    assert changed.paths.open_session("one", create=False)[0] == first
    assert (first.outputs / "report.csv").read_text() == "saved"
    assert changed.paths.for_session("new").outputs == tmp_path / "different-reports/default/new"


def test_sdk_also_persists_its_output_binding(runtime, tmp_path):
    original = configured(runtime, str(tmp_path / "reports"))
    build_graph(original, model=ScriptedModel(responses=[]))
    saved = original.paths.for_session("sdk")
    assert read_session_binding(saved).outputs == saved.outputs
    changed = configured(runtime, str(tmp_path / "different-reports"))
    build_graph(changed, model=ScriptedModel(responses=[]))
    assert changed.paths.for_session("sdk").outputs == saved.outputs


@pytest.mark.parametrize("entry", ["raw", "sdk", "explicit-session"])
async def test_agent_registers_once_then_reuses_binding_without_writes(runtime, binding_sql, entry):
    model = ScriptedModel(responses=[AIMessage(content="done")] * 2)
    if entry == "raw":
        agent = build_graph(runtime, model=model)
    else:
        options = (
            {"session": runtime.paths.for_session("sdk")} if entry == "explicit-session" else {}
        )
        agent = create_agent(runtime, model=model, **options)
    await agent.ainvoke({"messages": [("user", "first")]})
    assert binding_sql.count("BEGIN IMMEDIATE") == 1
    binding_sql.clear()
    if entry == "raw":
        agent = build_graph(runtime, model=model)
    await agent.ainvoke({"messages": [("user", "again")]})
    assert all(statement.startswith("SELECT ") for statement in binding_sql)
    assert not any("SELECT user_id, thread_id, output_dir FROM" in sql for sql in binding_sql)


@pytest.mark.parametrize("create", [False, True])
def test_existing_binding_opens_while_another_writer_holds_the_database(
    runtime, binding_sql, create
):
    session, _ = runtime.paths.open_session("saved", create=True)
    shutil.rmtree(session.outputs)
    connection = sqlite3.connect(runtime.paths.sessions_db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        binding_sql.clear()
        assert runtime.paths.open_session("saved", create=create)[0] == session
        assert {item.name for item in session.outputs.iterdir()} == {"artifacts"}
        assert all(statement.startswith("SELECT ") for statement in binding_sql)
        assert not any("SELECT user_id, thread_id, output_dir FROM" in sql for sql in binding_sql)
    finally:
        connection.rollback()
        connection.close()


async def test_rest_assembly_and_turns_register_each_binding_once(runtime, binding_sql):
    from test_api import client_for, query, sse, terminal

    async with client_for(runtime, ScriptedModel(responses=[AIMessage(content="done")] * 2)) as (
        client,
        app,
    ):
        binding_sql.clear()
        for text in ("first", "again"):
            response = await client.post("/api/v1/runs", json=query(text, "task"))
            assert terminal(sse(response)) == ["RUN_FINISHED"]
        assert binding_sql.count("BEGIN IMMEDIATE") == 1
        assert sum("INSERT INTO session_bindings" in sql for sql in binding_sql) == 1


def test_output_browsing_does_not_require_old_input_directories(runtime, tmp_path):
    source = tmp_path / "input"
    source.mkdir()
    paths = replace(runtime.paths, workspaces=(WorkspaceInput("input", source),))
    session, _ = paths.open_session("saved", create=True)
    source.rmdir()
    assert paths.for_session("saved") == session
    with pytest.raises(ValueError, match="Session binding is unreadable"):
        paths.open_session("saved", create=False)


def test_reject_rebinding_an_existing_session(runtime, tmp_path):
    session, _ = runtime.paths.open_session("saved", create=True)
    with pytest.raises(ValueError, match="already bound"):
        runtime.paths.open_session("saved", create=True, output_dir=tmp_path / "other")
    assert runtime.paths.for_session("saved") == session


def test_reject_output_root_that_is_a_file(runtime, tmp_path):
    path = tmp_path / "file"
    path.touch()
    with pytest.raises(ValueError, match="Expected a directory"):
        configured(runtime, str(path))


@pytest.mark.parametrize("value", ["~/reports", "absolute"])
def test_sdk_output_override_resolves_to_the_exact_directory(runtime, tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    requested = tmp_path / "absolute" if value == "absolute" else value
    expected = tmp_path / "user-home/reports" if value.startswith("~") else tmp_path / value
    prepared = configured(runtime, str(tmp_path / "configured-reports"))
    session, _ = prepared.paths.open_session("task", create=True, output_dir=requested)
    assert session.outputs == expected
    assert expected.is_dir()
    assert read_session_binding(session).outputs == expected
    assert prepared.paths.for_session("task").outputs == expected
    assert prepared.paths.for_session("another-task").outputs == (
        prepared.paths.output_root / "default/another-task"
    )


def test_sdk_output_binding_survives_restart_and_rejects_relocation(runtime, tmp_path):
    output = tmp_path / "reports/task"
    session, _ = runtime.paths.open_session("task", create=True, output_dir=output)
    (output / "report.txt").write_text("saved")
    restarted = configured(runtime, str(tmp_path / "different-default"))
    assert restarted.paths.open_session("task", create=False)[0] == session
    assert restarted.paths.open_session("task", create=True, output_dir=output)[0] == session
    with pytest.raises(ValueError, match="already bound to a different output directory"):
        restarted.paths.open_session("task", create=True, output_dir=tmp_path / "moved")
    assert not (tmp_path / "moved").exists()
    assert (output / "report.txt").read_text() == "saved"
    assert restarted.paths.for_session("task") == session


def test_sdk_output_override_does_not_create_missing_sessions_or_accept_files(runtime, tmp_path):
    output = tmp_path / "reports/task"
    assert runtime.paths.open_session("absent", create=False, output_dir=output) is None
    assert not output.exists()
    assert not runtime.paths.for_session("absent").root.exists()
    file = tmp_path / "file"
    file.touch()
    with pytest.raises(ValueError, match="Expected a directory"):
        runtime.paths.open_session("task", create=True, output_dir=file)
    assert find_session_binding(runtime.paths.for_session("task").root) is None


async def test_sdk_tasks_write_to_different_directories_with_the_same_runtime(runtime, tmp_path):
    prepared = configured(runtime, str(tmp_path / "configured-reports"))
    for task in ("task-a", "task-b"):
        output = tmp_path / "reports" / task
        session, workspaces = prepared.paths.open_session(task, create=True, output_dir=output)
        model = ScriptedModel(
            responses=[
                call("write_file", {"file_path": str(output / "report.txt"), "content": task}),
                call("execute", {"command": "cat report.txt"}),
                AIMessage(content="Saved"),
            ]
        )
        async with create_agent(
            prepared, session=session, input_workspaces=workspaces, model=model
        ) as agent:
            result = await agent.ainvoke(
                {"messages": [("user", "Write and read a report.")]},
                config={"configurable": {"thread_id": task}},
            )
        assert (output / "report.txt").read_text() == task
        tool_messages = [message for message in result["messages"] if message.type == "tool"]
        assert len(tool_messages) == 2
        assert all(message.status == "success" for message in tool_messages)
        assert task in tool_messages[-1].content
        assert session.artifacts == output / "artifacts"
        assert session.traces.is_relative_to(prepared.paths.home)
    assert not prepared.paths.output_root.exists()


@pytest.mark.parametrize("configured_root", [False, True])
def test_output_permissions_belong_to_caller(runtime, tmp_path, monkeypatch, configured_root):
    reports = tmp_path / "team-reports"
    reports.mkdir()
    reports.chmod(0o775)
    output = reports / "default/task" if configured_root else reports
    output.mkdir(parents=True, exist_ok=True)
    artifacts = output / "artifacts"
    artifacts.mkdir()
    output.chmod(0o775)
    artifacts.chmod(0o750)
    original_chmod = Path.chmod

    def only_owned_directories(path, *args, **kwargs):
        if path == reports or path.is_relative_to(reports):
            raise PermissionError("Caller directory is writable but not owned by the process")
        return original_chmod(path, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", only_owned_directories)
    prepared = configured(runtime, str(reports)) if configured_root else runtime
    with create_agent(prepared, model=ScriptedModel(responses=[])) as agent:
        for _ in range(2):
            agent._resolve("task", output_dir=None if configured_root else output)
    assert reports.stat().st_mode & 0o777 == 0o775
    assert output.stat().st_mode & 0o777 == 0o775
    assert artifacts.stat().st_mode & 0o777 == 0o750
    (output / "writable.txt").write_text("ok")
    session = prepared.paths.for_session("task")
    for path in (prepared.paths.state_dir, session.root, session.traces):
        assert path.stat().st_mode & 0o777 == 0o700


def test_new_output_tree_uses_normal_directory_creation_permissions(runtime, tmp_path):
    reference = tmp_path / "reference"
    reference.mkdir()
    prepared = configured(runtime, str(tmp_path / "reports"))
    session, _ = prepared.paths.open_session("new", create=True)
    for path in (
        prepared.paths.output_root,
        session.outputs.parent,
        session.outputs,
        session.artifacts,
    ):
        assert path.stat().st_mode & 0o777 == reference.stat().st_mode & 0o777


@pytest.mark.parametrize("factory", [create_agent, build_graph])
def test_explicit_session_outside_profile_is_rejected_before_writes(runtime, tmp_path, factory):
    root = tmp_path / "foreign/session"
    session = SessionPaths(root, tmp_path / "reports", root / "traces")
    with pytest.raises(ValueError, match="session.root"):
        factory(runtime, session=session, model=ScriptedModel(responses=[]))
    assert not root.parent.exists()
    assert not session.outputs.exists()
    assert not runtime.paths.for_session("session").root.exists()


@pytest.mark.parametrize("factory", [create_agent, build_graph])
def test_explicit_session_rejects_private_directory_overrides(runtime, tmp_path, factory):
    session = replace(runtime.paths.for_session("explicit"), traces=tmp_path / "traces")
    with pytest.raises(ValueError, match="standard Home path"):
        factory(runtime, session=session, model=ScriptedModel(responses=[]))
    assert not session.root.exists()
    assert not (tmp_path / "traces").exists()


def test_explicit_session_rejects_rebinding_without_creating_outputs(runtime, tmp_path):
    session, _ = runtime.paths.open_session("existing", create=True)
    changed = replace(session, outputs=tmp_path / "new-output")
    with pytest.raises(ValueError, match="already bound"):
        create_agent(runtime, session=changed, model=ScriptedModel(responses=[]))
    assert not changed.outputs.exists()
    assert runtime.paths.for_session("existing") == session


@pytest.mark.parametrize("inputs", [None, (), "custom"])
async def test_explicit_session_keeps_output_and_input_bindings(runtime, tmp_path, inputs):
    default_input = tmp_path / "default-input"
    custom_input = tmp_path / "custom-input"
    default_input.mkdir()
    custom_input.mkdir()
    defaults = (WorkspaceInput("default", default_input),)
    custom = (WorkspaceInput("custom", custom_input),)
    runtime = replace(runtime, paths=replace(runtime.paths, workspaces=defaults))
    inputs = custom if inputs == "custom" else inputs
    output = tmp_path / "explicit-output"
    session = replace(runtime.paths.for_session("explicit"), outputs=output)
    with create_agent(
        runtime,
        session=session,
        input_workspaces=inputs,
        model=ScriptedModel(
            responses=[
                call("execute", {"command": "printf expected > marker"}),
                AIMessage(content="done"),
            ]
        ),
    ) as agent:
        await agent.ainvoke({"messages": [("user", "write a marker")]})
        assert agent.context.session == session
    saved, workspaces = runtime.paths.open_session("explicit", create=False)
    assert saved == session
    assert workspaces == (defaults if inputs is None else inputs)
    assert (output / "marker").read_text() == "expected"
    assert not (session.root / "outputs").exists()


@pytest.mark.parametrize("factory", [create_agent, build_graph])
@pytest.mark.parametrize("requested", [None, "same", "alias", "different", "empty"])
def test_bound_session_only_accepts_matching_explicit_inputs(runtime, tmp_path, factory, requested):
    original = tmp_path / "original"
    other = tmp_path / "other"
    original.mkdir()
    other.mkdir()
    saved_inputs = (WorkspaceInput("saved", original),)
    session, _ = runtime.paths.open_session("bound", create=True, input_workspaces=saved_inputs)
    # Changed defaults are not explicit input overrides; omission must still resume.
    changed = replace(
        runtime, paths=replace(runtime.paths, workspaces=(WorkspaceInput("other", other),))
    )
    alias = tmp_path / "alias"
    alias.symlink_to(original, target_is_directory=True)
    inputs = {
        None: None,
        "same": (WorkspaceInput("renamed", original),),
        "alias": (WorkspaceInput("renamed", alias),),
        "different": (WorkspaceInput("other", other),),
        "empty": (),
    }[requested]
    model = ScriptedModel(responses=[])
    if requested in {"different", "empty"}:
        with pytest.raises(ValueError, match="bound to a different workspace list"):
            factory(changed, session=session, input_workspaces=inputs, model=model)
    else:
        factory(changed, session=session, input_workspaces=inputs, model=model)
    assert read_session_binding(session).workspaces == saved_inputs
    assert not model.requests


async def test_sdk_lazy_sessions_retain_and_validate_explicit_inputs(runtime, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    explicit = (WorkspaceInput("data", source),)
    async with create_agent(
        runtime,
        input_workspaces=explicit,
        model=ScriptedModel(responses=[AIMessage(content="done")]),
    ) as agent:
        await agent.ainvoke({"messages": [("user", "go")]}, {"configurable": {"thread_id": "task"}})
    session = runtime.paths.for_session("task")
    assert read_session_binding(session).workspaces == explicit
    async with create_agent(
        runtime, input_workspaces=(), model=ScriptedModel(responses=[])
    ) as agent:
        with pytest.raises(ValueError, match="bound to a different workspace list"):
            await agent.ainvoke({}, {"configurable": {"thread_id": "task"}})
        assert not agent._active
    assert read_session_binding(session).workspaces == explicit


def test_explicit_inputs_preserve_order_and_empty_bindings(runtime, tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    inputs = (WorkspaceInput("left", left), WorkspaceInput("right", right))
    runtime.paths.open_session("ordered", create=True, input_workspaces=inputs)
    with pytest.raises(ValueError, match="different workspace list"):
        runtime.paths.open_session("ordered", create=True, input_workspaces=inputs[::-1])
    runtime.paths.open_session("empty", create=True, input_workspaces=())
    assert runtime.paths.open_session("empty", create=True, input_workspaces=())[1] == ()
    with pytest.raises(ValueError, match="different workspace list"):
        runtime.paths.open_session("empty", create=True, input_workspaces=inputs)


def test_configured_output_error_names_its_file(runtime):
    with pytest.raises(ValueError, match=r"outputs\.yaml: dataagent\.output_root: .*absolute path"):
        configured(runtime, "reports")


def test_other_home_has_no_ownership_but_still_rejects_nonempty_derived_outputs(runtime, tmp_path):
    first = replace(runtime.paths, output_root=tmp_path / "reports")
    second = replace(first, home=tmp_path / "other-home")
    session, _ = first.open_session("task-a", create=True)
    (session.outputs / "report.csv").write_text("first")
    with pytest.raises(ValueError, match="no session owns"):
        second.open_session("task-a", create=True)
    assert find_session_binding(second.for_session("task-a").root) is None
    assert (session.outputs / "report.csv").read_text() == "first"


def test_two_threads_cannot_share_an_explicit_output_directory(runtime, tmp_path):
    output = tmp_path / "shared"
    runtime.paths.open_session("task-a", create=True, output_dir=output)
    with pytest.raises(ValueError, match="task-a"):
        runtime.paths.open_session("task-b", create=True, output_dir=output)
    assert find_session_binding(runtime.paths.for_session("task-b").root) is None


def test_explicit_output_adopts_existing_unowned_files(runtime, tmp_path):
    output = tmp_path / "existing-reports"
    output.mkdir()
    (output / "old.txt").write_text("keep")
    session, _ = runtime.paths.open_session("task", create=True, output_dir=output)
    assert (output / "old.txt").read_text() == "keep"
    assert {item.name for item in output.iterdir()} == {"old.txt", "artifacts"}
    assert read_session_binding(session).outputs == output


def test_configured_output_rejects_existing_unowned_files(runtime, tmp_path):
    paths = replace(runtime.paths, output_root=tmp_path / "reports")
    stale = paths.for_session("task").outputs
    stale.mkdir(parents=True)
    (stale / "old.txt").write_text("unknown")
    with pytest.raises(ValueError, match="no session owns"):
        paths.open_session("task", create=True)
    assert {item.name for item in stale.iterdir()} == {"old.txt"}
    assert find_session_binding(paths.for_session("task").root) is None


def test_resume_reclaims_a_recreated_output_directory(runtime, tmp_path):
    output = tmp_path / "reports/task"
    session, _ = runtime.paths.open_session("task", create=True, output_dir=output)
    shutil.rmtree(output)
    assert runtime.paths.open_session("task", create=False)[0] == session
    assert {item.name for item in output.iterdir()} == {"artifacts"}
    assert read_session_binding(session).outputs == output


@pytest.mark.parametrize("reverse", [False, True])
def test_nested_output_bindings_are_rejected(runtime, tmp_path, reverse):
    parent, child = tmp_path / "out", tmp_path / "out/sub"
    first, second = (child, parent) if reverse else (parent, child)
    runtime.paths.open_session("one", create=True, output_dir=first)
    with pytest.raises(ValueError, match="used by session"):
        runtime.paths.open_session("two", create=True, output_dir=second)


def test_symlink_alias_cannot_claim_bound_output(runtime, tmp_path):
    output = tmp_path / "out"
    runtime.paths.open_session("one", create=True, output_dir=output)
    alias = tmp_path / "alias"
    alias.symlink_to(output, target_is_directory=True)
    with pytest.raises(ValueError, match="used by session"):
        runtime.paths.open_session("two", create=True, output_dir=alias)


@pytest.mark.parametrize(
    "location",
    [
        "runtime",
        "traces",
        "old-default",
        "ancestor",
        "input",
        "input-child",
        "input-parent",
    ],
)
def test_reject_private_state_and_input_overlap(runtime, tmp_path, location):
    source = tmp_path / "inputs/source"
    source.mkdir(parents=True)
    paths = replace(runtime.paths, workspaces=(WorkspaceInput("source", source),))
    output = {
        "runtime": paths.state_dir,
        "traces": paths.for_session("one").traces,
        "old-default": paths.for_session("one").root / "outputs",
        "ancestor": paths.home,
        "input": source,
        "input-child": source / "out",
        "input-parent": source.parent,
    }[location]
    with pytest.raises(ValueError, match="overlaps"):
        paths.open_session("one", create=True, output_dir=output)
    assert paths.open_session("one", create=False) is None


@pytest.mark.parametrize(
    "relative", [".", "..", "plugins", "plugins/common", "skills", "hooks", "custom"]
)
@pytest.mark.parametrize("configured_root", [False, True])
def test_outputs_cannot_use_home_outside_outputs(runtime, relative, configured_root):
    output = (runtime.paths.home / relative).resolve()
    paths = replace(runtime.paths, output_root=output) if configured_root else runtime.paths
    options = {} if configured_root else {"output_dir": output}
    # A root above Home can produce a final directory outside Home; use a thread
    # under Home in that case so the candidate itself overlaps the protected area.
    if configured_root and relative == "..":
        paths = replace(paths, user_id=runtime.paths.home.name)
    with pytest.raises(ValueError, match="overlaps Home"):
        paths.open_session("task", create=True, **options)
    assert find_session_binding(paths.for_session("task").root) is None


@pytest.mark.parametrize("name", ["config.yaml", ".env"])
def test_output_override_cannot_replace_home_configuration(runtime, name):
    output = runtime.paths.home / name
    output.write_text("preserve configuration")
    with pytest.raises(ValueError, match="Expected a directory"):
        runtime.paths.open_session("task", create=True, output_dir=output)
    assert output.read_text() == "preserve configuration"


@pytest.mark.parametrize("location", ["outputs", "outputs/default", "alias"])
def test_symlinks_cannot_turn_home_extensions_into_outputs(runtime, tmp_path, location):
    extensions = runtime.paths.home / "plugins"
    alias = tmp_path / "alias" if location == "alias" else runtime.paths.home / location
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(extensions, target_is_directory=True)
    with pytest.raises(ValueError, match="overlaps Home"):
        runtime.paths.open_session("task", create=True, output_dir=alias / "result")
    assert not (extensions / "result").exists()


@pytest.mark.parametrize("create", [False, True])
def test_existing_binding_revalidates_home_boundary_without_rebinding(runtime, create):
    session, _ = runtime.paths.open_session("task", create=True)
    shutil.rmtree(session.outputs)
    session.outputs.symlink_to(runtime.paths.home / "plugins", target_is_directory=True)
    with pytest.raises(ValueError, match="overlaps Home"):
        runtime.paths.open_session("task", create=create)
    assert read_session_binding(session).outputs == session.outputs
    assert not (runtime.paths.home / "plugins/artifacts").exists()


def test_binding_lookup_is_read_only_and_registration_writes_only_database(runtime):
    session = runtime.paths.for_session("new")
    assert runtime.paths.open_session("new", create=False) is None
    assert not runtime.paths.sessions_db.exists()
    assert not session.root.exists()
    runtime.paths.open_session("new", create=True)
    assert runtime.paths.sessions_db.exists()
    assert read_session_binding(session).outputs == session.outputs
    assert {item.name for item in session.root.iterdir()} == {"traces"}
    assert {item.name for item in session.outputs.iterdir()} == {"artifacts"}


def test_concurrent_claims_only_register_one_owner(runtime, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    def claim(thread):
        barrier.wait()
        try:
            runtime.paths.open_session(thread, create=True, output_dir=tmp_path / "out")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ["one", "two"])) == [False, True]
    with sqlite3.connect(runtime.paths.sessions_db) as db:
        assert db.execute("SELECT COUNT(*) FROM session_bindings").fetchone()[0] == 1
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_cross_home_explicit_adoption_is_allowed(runtime, tmp_path):
    output = tmp_path / "shared"
    runtime.paths.open_session("one", create=True, output_dir=output)
    other = replace(runtime.paths, home=tmp_path / "other-home")
    assert other.open_session("two", create=True, output_dir=output)[0].outputs == output


def test_mac_case_only_overlap_is_conservatively_rejected(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr("dataagent.bootstrap.paths.sys.platform", "darwin")
    runtime.paths.open_session("one", create=True, output_dir=tmp_path / "Out")
    with pytest.raises(ValueError, match="used by session"):
        runtime.paths.open_session("two", create=True, output_dir=tmp_path / "out")


def test_default_output_symlink_cannot_expose_private_state(runtime):
    session = runtime.paths.for_session("one")
    session.traces.mkdir(parents=True)
    session.outputs.parent.mkdir(parents=True)
    session.outputs.symlink_to(session.traces, target_is_directory=True)
    with pytest.raises(ValueError, match="overlaps Home"):
        runtime.paths.open_session("one", create=True)


async def test_sdk_and_rest_own_separate_tables_in_one_database(runtime):
    from dataagent.server.sessions import Sessions

    runtime.paths.state_dir.mkdir(parents=True, exist_ok=True)
    async with Sessions(runtime.paths.sessions_db) as sessions:
        assert runtime.paths.open_session("unknown", create=False) is None
        runtime.paths.open_session("sdk", create=True)
        async with sessions.db.execute("SELECT COUNT(*) FROM sessions") as cursor:
            assert (await cursor.fetchone())[0] == 0
    assert read_session_binding(runtime.paths.for_session("sdk")).thread_id == "sdk"
