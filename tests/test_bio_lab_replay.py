"""Minimal offline smoke checks for Bio Lab assembly and evaluation setup."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location(
    "bio_replay", Path(__file__).parent / "e2e/bio_lab/run.py"
)

replay = importlib.util.module_from_spec(SPEC)

SPEC.loader.exec_module(replay)


async def test_sdk_bootstrap_with_generated_config(tmp_path, monkeypatch):
    import httpx
    from conftest import ScriptedModel, call
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from dataagent import LaunchOptions, create_agent, prepare_runtime
    from dataagent.semantic_client import SemanticServiceClient

    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    db = tmp_path / "fixture.sqlite"
    sqlite3.connect(db).close()
    config = tmp_path / "config.yaml"
    config.write_text(json.dumps(replay.config_for(db)))
    replay.install_scenario(tmp_path / "home")
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))
    data = json.loads((replay.FIXTURES / "config/metavisor_responses.json").read_text())
    with SemanticServiceClient(
        "https://fixture.invalid", transport=httpx.MockTransport(replay.fixture_handler(data, []))
    ) as client:
        model = ScriptedModel(
            responses=[
                call("data-qa__get_ontology_description_tool", {}),
                call("data-qa__request_human_feedback", {"question": "继续吗？"}),
                AIMessage(content="完成"),
            ]
        )
        with create_agent(
            runtime, model=model, semantic_client=client, checkpointer=InMemorySaver()
        ) as agent:
            transcript = []
            state = await replay.invoke_with_feedback(
                agent,
                "查看 schema",
                "smoke",
                ["继续"],
                transcript,
                instructions="先查 schema，再确认。",
            )
            assert state["messages"][-1].content == "完成"
            schema = next(
                m
                for m in state["messages"]
                if getattr(m, "name", None) == "data-qa__get_ontology_description_tool"
            )
            assert json.loads(schema.content)["tables"]
            assert transcript[0]["consumed"]
        disabled = replay.config_for(db)
        disabled["plugins"]["enabled"] = ["nl2sql"]
        config.write_text(json.dumps(disabled))
        runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))
        model = ScriptedModel(responses=[AIMessage(content="ok")])
        with create_agent(runtime, model=model, semantic_client=client) as agent:
            await agent.ainvoke({"messages": [("user", "hello")]})
        assert not {
            "data-qa__get_ontology_description_tool",
            "data-qa__request_human_feedback",
        }.intersection(model.tool_schemas[-1])
    assert runtime.settings.dataagent.entry is None


async def test_prepare_all_is_self_contained_and_needs_no_model(tmp_path, monkeypatch):
    for name in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    args = SimpleNamespace(
        group="all",
        case=None,
        output=tmp_path / "prepared",
        env_file=None,
        semantic_url=None,
        timeout=30,
        prepare_only=True,
    )
    assert await replay.run_groups(args) == 0
    for group, count in (("TC", 22), ("TC-W", 15)):
        output = args.output / group
        results = json.loads((output / "results.json").read_text())
        assert len(results) == count
        assert all(item["status"] == "prepared" for item in results)
        assert not (output / "home/plugins").exists()
        for item in results:
            config = json.loads((output / item["key"] / "config.yaml").read_text())
            assert config["plugins"]["enabled"] == ["nl2sql", "data-qa"]
            assert Path(config["plugins"]["config"]["nl2sql"]["database"]["path"]).is_file()
            assert Path(
                config["plugins"]["config"]["nl2sql"]["perceptor"]["user_sql_rules"]
            ).is_file()


async def test_ontology_ignores_malformed_table_entries():
    from unittest.mock import Mock

    import pytest
    from langchain_core.tools import ToolException

    spec = importlib.util.spec_from_file_location(
        "ontology_review", Path(__file__).parents[1] / "dataagent/plugins/data-qa/tools/ontology.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = Mock()
    context = SimpleNamespace(
        get_plugin_config=lambda _: {"database": "bio_lab"}, semantic_client=client
    )
    for tables, names in [
        ([{"samples": "Samples"}, "bad", None, 7, {}], ["samples"]),
        (None, []),
        ([], []),
    ]:
        client.reset_mock()
        client.get_table_list.return_value = tables
        client.get_table_columns_info.return_value = {}
        client.get_joinable_tables.return_value = []
        result = await module.get_ontology_description_tool.coroutine(
            SimpleNamespace(context=context)
        )
        assert list(result["columns"]) == names
        assert client.get_table_columns_info.call_count == len(names)
        assert client.get_joinable_tables.call_count == bool(names)
    client.get_table_list.return_value = {"tables": []}
    with pytest.raises(ToolException, match="table list"):
        await module.get_ontology_description_tool.coroutine(SimpleNamespace(context=context))


def test_shell_evidence_accepts_only_successful_recognized_commands(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage

    root = tmp_path / "skills"
    script = root / "create-neutralization-experiment/scripts/insert_neutralization_experiment.py"
    script.parent.mkdir(parents=True)
    script.touch()
    command = f"cd {script.parent.parent} && python scripts/{script.name}"
    for cmd, stdout, code, qualifies, category in [
        (command, '{"id": 1}', 0, True, "skill_script"),
        (command + " && echo extra", "done", 0, False, "other"),
        (command, "failed", 1, False, "skill_script"),
        (command, '{"error": "failed"}', 0, False, "skill_script"),
        (command, "<no output>", 0, False, "skill_script"),
        ("sqlite3 db.sqlite 'SELECT id FROM samples'", "<no output>", 0, True, "database_read"),
        (command, "done", None, False, "skill_script"),
    ]:
        footer = (
            f"\n[Command {'succeeded' if code == 0 else 'failed'} with exit code {code}]"
            if code is not None
            else ""
        )
        messages = [
            AIMessage(
                content="",
                tool_calls=[{"id": "shell", "name": "execute", "args": {"command": cmd}}],
            ),
            ToolMessage(content=stdout + footer, tool_call_id="shell"),
        ]
        evidence = replay.collect_shell_evidence(messages, skill_root=root)[0]
        assert (evidence["qualifies"], evidence["category"], evidence["exit_code"]) == (
            qualifies,
            category,
            code,
        )
