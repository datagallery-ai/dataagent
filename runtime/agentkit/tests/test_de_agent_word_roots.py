"""Word-root migration contracts, exercised through the native plugin loader."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from dataagent.declarations import PluginSpec
from dataagent.extensions.hooks import compile_hooks
from dataagent.extensions.loading import PythonLoader
from dataagent.extensions.tools import collect_tools

PLUGIN = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"


def context(tmp_path, config=None):
    return SimpleNamespace(
        session=SimpleNamespace(outputs=tmp_path),
        get_plugin_config=lambda _: {"word_roots": config or {}},
        get_model=lambda _: None,
    )


def matched(items, **kwargs):
    return {"results": [
        {"index": i, "english_name": item["english_name"], "found": True,
         "match_type": "exact", "matches": [], "candidates": []}
        for i, item in enumerate(items)
    ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("filename,sql,names", [
    ("create_user.sql", "CREATE TABLE db.user_id (user_id STRING) PARTITIONED BY (pt_d STRING)",
     ["user_id", "pt_d"]),
    ("alter_user.sql", "ALTER TABLE db.legacy ADD COLUMNS (user_id STRING, user_cnt BIGINT)",
     ["user_id", "user_cnt"]),
    ("alter_user.sql", "ALTER TABLE db.legacy DROP COLUMN old_col", []),
])
async def test_hook_extracts_only_new_names(tmp_path, monkeypatch, filename, sql, names):
    hook = PythonLoader().load(PLUGIN, "hooks/word_roots.py:check_word_roots")
    validate = AsyncMock(return_value={"pass": True})
    monkeypatch.setitem(hook.__globals__, "validate_identifier_word_roots", validate)
    (tmp_path / filename).write_text(sql)
    request = SimpleNamespace(tool_call={"id": "call", "args": {"file_path": filename}},
                              runtime=SimpleNamespace(context=context(tmp_path)))
    assert await hook(request, ToolMessage(content="written", tool_call_id="call"), params={}) is None
    if names:
        assert validate.await_args.args == (names,)
    else:
        validate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("report,status,fragment", [
    (None, "success", "warning"),
    ({}, "success", "warning"),
    ({"validation_status": "skipped", "results": []}, "success", "warning"),
    ({"pass": "true"}, "success", "warning"),
    (RuntimeError("service unavailable"), "success", "warning"),
    (OSError("connection failed"), "success", "warning"),
    ({"pass": False, "names": [], "error_msg": "dictionary missing"}, "success", "warning"),
    ({"pass": False, "names": [{"name": "usr", "failed_roots": [{"index": 0}]}],
      "error_msg": ""}, "error", "report"),
])
async def test_hook_preserves_command_state_and_written_file(tmp_path, monkeypatch, report, status, fragment):
    hook = PythonLoader().load(PLUGIN, "hooks/word_roots.py:check_word_roots")
    validate = AsyncMock(side_effect=report) if isinstance(report, Exception) else AsyncMock(return_value=report)
    monkeypatch.setitem(hook.__globals__, "validate_identifier_word_roots", validate)
    path = tmp_path / "create_user.sql"
    path.write_text("CREATE TABLE db.usr (user_id STRING)")
    request = SimpleNamespace(tool_call={"id": "call", "args": {"file_path": path.name}},
                              runtime=SimpleNamespace(context=context(tmp_path)))
    original = ToolMessage(content="written", tool_call_id="call")
    unrelated = ToolMessage(content="other", tool_call_id="other")
    result = await hook(request, Command(update={"messages": [original, unrelated], "files": {"a": "b"}}), params={})
    assert result.update["files"] == {"a": "b"}
    message = result.update["messages"][0]
    assert message.status == status and fragment in message.content
    assert message.content.startswith("written\n")
    assert result.update["messages"][1] is unrelated
    assert path.read_text() == "CREATE TABLE db.usr (user_id STRING)"


@pytest.mark.asyncio
async def test_path_resolution_failure_warns_without_losing_command_state(tmp_path, monkeypatch):
    hook = PythonLoader().load(PLUGIN, "hooks/word_roots.py:check_word_roots")
    def fail(*args):
        raise ValueError("Cannot resolve output path")
    monkeypatch.setitem(hook.__globals__, "output_file", fail)
    request = SimpleNamespace(tool_call={"id": "call", "args": {"file_path": "create_user.sql"}},
                              runtime=SimpleNamespace(context=context(tmp_path)))
    original = ToolMessage(content="written", tool_call_id="call", id="message-id")
    result = await hook(request, Command(update={"messages": [original], "files": {"a": "b"}}), params={})
    message = result.update["messages"][0]
    assert message.status == "success" and message.id == original.id
    payload = json.loads(message.content.split("\n", 1)[1])
    assert payload["word_roots_valid"] is None
    assert "Cannot resolve output path" in payload["warning"]
    assert result.update["files"] == {"a": "b"}


@pytest.mark.asyncio
async def test_parse_failure_and_failed_write_do_not_call_validator(tmp_path, monkeypatch):
    hook = PythonLoader().load(PLUGIN, "hooks/word_roots.py:check_word_roots")
    validate = AsyncMock()
    monkeypatch.setitem(hook.__globals__, "validate_identifier_word_roots", validate)
    (tmp_path / "create_bad.sql").write_text("CREATE TABLE db.bad (")
    request = SimpleNamespace(tool_call={"id": "call", "args": {"file_path": "create_bad.sql"}},
                              runtime=SimpleNamespace(context=context(tmp_path)))
    result = await hook(request, ToolMessage(content="written", tool_call_id="call"), params={})
    assert result.status == "error"
    assert await hook(request, ToolMessage(content="failed", status="error", tool_call_id="call"), params={}) is None
    validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_chunking_and_shared_failures_keep_name_local_indices(tmp_path, monkeypatch):
    validate = PythonLoader().load(PLUGIN, "utils/word_roots.py:validate_identifier_word_roots")
    async def batch(items, **kwargs):
        response = matched(items)
        for item in response["results"]:
            if item["english_name"] == "c22":
                item.update(found=False, match_type=None, candidates=[{"record": {"root": "c23"}}])
        return response
    mock = AsyncMock(side_effect=batch)
    monkeypatch.setitem(validate.__globals__, "validate_word_roots", mock)
    names = [f"c{i}" for i in range(25)] + ["c22_c22", "c22_c1"]
    result = await validate(names, _tool_context=None)
    assert [len(call.kwargs["items"]) for call in mock.await_args_list] == [20, 5]
    assert result["pass"] is False
    assert [r["name"] for r in result["names"] if not r["pass"]] == ["c22", "c22_c22", "c22_c1"]
    assert [[r["index"] for r in item["failed_roots"]] for item in result["names"][-2:]] == [[0, 1], [0]]
    assert all(root["english_name"] == "c22" for item in result["names"] for root in item["failed_roots"])


@pytest.mark.asyncio
async def test_raw_tool_csv_matching_and_async_candidates(tmp_path):
    tool = PythonLoader().load(PLUGIN, "tools/word_roots.py:validate_word_roots")
    csv = tmp_path / "roots.csv"
    csv.write_text("词根英文名,词根中文名,描述,词根类型,特殊词根规则\nuser,用户,,普通词根,\nn,数字,,特殊词根,^n[0-9]+$\n")
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps(
        {"results": [{"query_id": "q2", "candidates": [{"id": "row-2", "score": 90}]}]}
    ))))
    ctx = context(tmp_path, {"csv_path": str(csv), "llm_call_params": {}})
    ctx.get_model = lambda name: model
    result = await tool([{"english_name": "user"}, {"english_name": "n123"},
                         {"english_name": "usr", "chinese_description": "用户"}], SimpleNamespace(context=ctx))
    assert [r["match_type"] for r in result["results"]] == ["exact", "regex", None]
    assert result["results"][2]["candidates"][0]["record"]["词根英文名"] == "user"
    assert "用户" in model.ainvoke.await_args.args[0][-1]["content"]
    missing = await tool([{"english_name": "user"}], SimpleNamespace(context=context(tmp_path, {"csv_path": str(tmp_path / "missing.csv")})))
    assert missing["validation_status"] == "skipped"
    invalid = await tool([], SimpleNamespace(context=ctx))
    assert invalid["validation_status"] == "error"


@pytest.mark.asyncio
async def test_registered_hooks_skip_root_lookup_on_structural_errors(tmp_path, monkeypatch):
    loader = PythonLoader()
    spec = PluginSpec.model_validate_json((PLUGIN / ".plugin.json").read_text())
    validate = loader.load(PLUGIN, "utils/word_roots.py:validate_identifier_word_roots")
    mock = AsyncMock()
    monkeypatch.setitem(validate.__globals__, "validate_word_roots", mock)
    hooks = compile_hooks(
        [(PLUGIN, entry) for entry in spec.hooks
         if entry.event == "after_tool" and entry.matcher == "write_file"],
        loader, agent_name="dataagent",
    )
    (tmp_path / "create_invalid.sql").write_text("CREATE TABLE db.users (user_id STRING)")
    request = SimpleNamespace(
        tool_call={"id": "call", "name": "write_file", "args": {"file_path": "create_invalid.sql"}},
        runtime=SimpleNamespace(context=context(tmp_path)),
    )
    async def written(request):
        return ToolMessage(content="written", tool_call_id="call")
    handler = written
    for middleware in reversed(hooks):
        async def handler(request, middleware=middleware, inner=handler):
            return await middleware.awrap_tool_call(request, inner)
    result = await handler(request)
    assert result.status == "error" and "ddl_valid" in result.content
    mock.assert_not_awaited()


def test_native_tool_schema_hides_runtime():
    spec = PluginSpec.model_validate_json((PLUGIN / ".plugin.json").read_text())
    registry = {}
    collect_tools(PLUGIN, spec, PythonLoader(), registry)
    schema = registry["de-agent__validate_word_roots"].tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"items", "k", "llm_batch_size"}
    assert schema["required"] == ["items"]
