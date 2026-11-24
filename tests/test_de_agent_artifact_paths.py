"""Out-of-session artifacts produce recoverable tool results."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from dataagent.extensions.loading import PythonLoader

PLUGIN = Path(__file__).resolve().parents[1] / "dataagent/plugins/de-agent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hook_name,filename",
    [
        ("check_ddl", "create_bad.sql"),
        ("check_insert_syntax", "insert_bad.sql"),
        ("check_word_roots", "create_bad.sql"),
    ],
)
@pytest.mark.parametrize("escape", ["absolute", "parent", "symlink"])
async def test_after_write_path_error_preserves_state(tmp_path, hook_name, filename, escape):
    root = tmp_path / "outputs"
    root.mkdir()
    outside = tmp_path / filename
    outside.write_text("do not read or modify")
    if escape == "symlink":
        (root / filename).symlink_to(outside)
        value = filename
    else:
        value = str(outside) if escape == "absolute" else f"../{filename}"
    context = SimpleNamespace(session=SimpleNamespace(outputs=root))
    request = SimpleNamespace(
        tool_call={"id": "call", "args": {"file_path": value}},
        runtime=SimpleNamespace(context=context),
    )
    original = ToolMessage(content="written", tool_call_id="call", id="message-id")
    other = ToolMessage(content="other", tool_call_id="other")
    entry = "word_roots" if hook_name == "check_word_roots" else "files"
    hook = PythonLoader().load(PLUGIN, f"hooks/{entry}.py:{hook_name}")
    result = hook(
        request, Command(update={"messages": [original, other], "files": {"x": "y"}}), params={}
    )
    if hook_name == "check_word_roots":
        result = await result
    message = result.update["messages"][0]
    assert message.status == "error" and message.id == original.id
    assert str(root) in message.content and value in message.content
    assert "artifact_outside_outputs" in message.content
    assert result.update["messages"][1] is other
    assert result.update["files"] == {"x": "y"}
    assert outside.read_text() == "do not read or modify"


def test_guard_insert_returns_error(tmp_path):
    hook = PythonLoader().load(PLUGIN, "hooks/files.py:guard_insert")
    context = SimpleNamespace(session=SimpleNamespace(outputs=tmp_path / "outputs"))
    request = SimpleNamespace(
        tool_call={"id": "call", "args": {"file_path": str(tmp_path / "insert_bad.sql")}},
        runtime=SimpleNamespace(context=context),
    )
    result = hook(request, params={})
    assert result.status == "error"
    assert json.loads(result.content)["error"] == "artifact_outside_outputs"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,argument,flag",
    [
        ("trial_compute_table", "dml_path", "passed"),
        ("dataops_validate_data", "sql_file_path", "ok"),
    ],
)
async def test_dataops_returns_path_error_without_execution(
    tmp_path, monkeypatch, name, argument, flag
):
    from contextlib import nullcontext

    tool = PythonLoader().load(PLUGIN, f"tools/dataops.py:{name}")
    monkeypatch.setitem(tool.__globals__, "dataops_settings", nullcontext)
    monkeypatch.setitem(tool.__globals__, "dataops_options", lambda context: {"enabled": True})
    context = SimpleNamespace(session=SimpleNamespace(outputs=tmp_path / "outputs"))
    result = await tool(SimpleNamespace(context=context), **{argument: str(tmp_path / "bad.sql")})
    assert result[flag] is False
    assert result["reason"] == "artifact_outside_outputs"
