"""Opt-in model and deployed semantic-service acceptance; no scripted models.

The env file named by DATAAGENT_DE_AGENT_LIVE_ENV must define LLM_MODEL, LLM_BASE_URL and LLM_API_KEY.
"""

import asyncio
import json
import os
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from dataagent import LaunchOptions, create_agent, prepare_runtime
from dataagent.agent import load_extensions
from dataagent.extensions.loading import PythonLoader
from dataagent.models import AgentContext
from dataagent.semantic_client import SemanticServiceClient


def _live_runtime(tmp_path):
    env_file = os.environ.get("DATAAGENT_DE_AGENT_LIVE_ENV")
    semantic_url = os.environ.get("DATAAGENT_DE_AGENT_LIVE_URL")
    if not env_file or not semantic_url:
        pytest.skip("Set DATAAGENT_DE_AGENT_LIVE_ENV and DATAAGENT_DE_AGENT_LIVE_URL")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "$env{LLM_MODEL}",
                        "base_url": "$env{LLM_BASE_URL}",
                        "api_key": "$env{LLM_API_KEY}",
                        "temperature": 0,
                    }
                },
                "dataagent": {"limits": {"timeout_seconds": 180, "model_calls_per_agent": 25}},
                "semantic": {"base_url": semantic_url, "timeout_seconds": 30},
                "plugins": {
                    "enabled": ["de-agent"],
                    "config": {"de-agent": {"dataops": {"enabled": False}}},
                },
            }
        )
    )
    return prepare_runtime(LaunchOptions(cwd=tmp_path, config=config, env_file=Path(env_file)))


@pytest.mark.live
async def test_de_agent_native_tasks_generate_artifact(tmp_path):
    """Drive metadata recall, DDL feedback, compiled NL2SQL and delivery through the root Agent."""
    runtime = _live_runtime(tmp_path)
    agent = create_agent(runtime, session=runtime.paths.for_session("real-data-engineering"))
    try:
        response = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "验证数据工程流程：唯一来源表 temp.tmp_dwd_adv_1688_intentions_crowd_ds，"
                            "统计全表 COUNT(*) 为 row_total，不加任何过滤或 join。目标 temp.ads_plugin_probe_ds。"
                            "使用 Hive EXTERNAL TABLE，row_total BIGINT COMMENT '总行数'，"
                            "分区 pt_d STRING COMMENT '日期分区'，静态分区值 '20261020'，STORED AS ORC。"
                            "先用 task 的 metadata-recall 获取该表实际 schema，再 write_file 写 create DDL，"
                            "再通过 task 的 nl2sql 生成 INSERT，最后 check_and_repair_sql。"
                            "只生成文件不执行 SQL。按此明确需求直接完成，不需要确认。"
                        ),
                    }
                ]
            },
            config={"recursion_limit": 100},
        )
        calls = [
            call
            for message in response.get("messages", [])
            for call in getattr(message, "tool_calls", [])
        ]
        tasks = [
            call.get("args", {}).get("subagent_type")
            for call in calls
            if call.get("name") == "task"
        ]
        assert "metadata-recall" in tasks and "nl2sql" in tasks
        replies = []
        for message in response.get("messages", []):
            if isinstance(message, ToolMessage) and message.name == "task":
                try:
                    replies.append(json.loads(message.content))
                except (ValueError, TypeError):
                    continue
        generated = [reply for reply in replies if reply.get("artifact_path")]
        assert generated, response.get("messages", [])[-1].content
        result = generated[-1]
        assert not result.get("error") and result.get("executed") is False
        artifact = Path(result.get("artifact_path"))
        assert artifact.is_relative_to(agent.context.session.outputs)
        assert "INSERT" in artifact.read_text().upper()
        assert list((agent.context.session.outputs / "metadata").glob("schema_*.json"))
        assert any(call.get("name") == "de-agent__check_and_repair_sql" for call in calls)
        validations = [
            json.loads(message.content)
            for message in response.get("messages", [])
            if isinstance(message, ToolMessage)
            and message.name == "de-agent__check_and_repair_sql"
        ]
        assert validations[-1].get("passed"), validations[-1]
        assert validations[-1].get("status") == "PASS", validations[-1]
        assert not list(runtime.paths.for_session("another-session").outputs.rglob("*.sql"))
    finally:
        await agent.aclose()


@pytest.mark.live
async def test_real_parallel_nl2sql_sessions_and_missing_evidence(tmp_path):
    """Use real generation in parallel sessions and reject an unevidenced task before saving SQL."""
    runtime = _live_runtime(tmp_path)
    root = Path(__file__).resolve().parents[1] / "builtin-plugins/de-agent"
    save = PythonLoader().load(root, "utils/evidence.py:save_evidence")
    with SemanticServiceClient.from_settings(runtime.settings.semantic) as client:
        source = "temp.tmp_dwd_adv_1688_intentions_crowd_ds"
        metadata = await asyncio.to_thread(client.get_table_columns_info, source)
        contexts = [
            AgentContext(runtime, semantic_client=client, session=runtime.paths.for_session(name))
            for name in ("parallel-a", "parallel-b")
        ]
        children = []
        for context in contexts:
            compiled = load_extensions(
                runtime, context=context, model_resolver=context.get_model, semantic_client=client
            )
            child = next(
                child.get("runnable")
                for child in compiled.get("subagents")
                if child.get("name") == "nl2sql"
            )
            children.append(child)
        failed = await children[0].ainvoke({"messages": [HumanMessage(content="Generate SQL")]})
        assert json.loads(failed.get("messages")[0].content).get("error")
        assert not list(contexts[0].session.outputs.rglob("insert_*.sql"))
        queries = []
        for index, context in enumerate(contexts):
            save(context, "schema", source, metadata)
            target = f"temp.ads_parallel_{index}_ds"
            folder = context.session.outputs / "sql"
            folder.mkdir(parents=True)
            (folder / f"create_{target}.sql").write_text(
                f"CREATE EXTERNAL TABLE IF NOT EXISTS {target} (\n"
                "row_total BIGINT COMMENT '总行数'\n) COMMENT '总行数验证'\n"
                "PARTITIONED BY (pt_d STRING COMMENT '日期分区') STORED AS ORC;"
            )
            queries.append(
                {
                    "messages": [
                        HumanMessage(
                            content=json.dumps(
                                {
                                    "question": (
                                        f"来源 {source} 全表 COUNT(*) 为 row_total，不加过滤或 join，"
                                        f"INSERT OVERWRITE 目标 {target}，静态分区 pt_d='20261020'。仅生成不执行。"
                                    ),
                                    "source_table": [source],
                                },
                                ensure_ascii=False,
                            )
                        )
                    ]
                }
            )
        responses = await asyncio.gather(
            *(child.ainvoke(query) for child, query in zip(children, queries, strict=True))
        )
        for index, (context, response) in enumerate(zip(contexts, responses, strict=True)):
            payload = json.loads(response.get("messages")[0].content)
            assert not payload.get("error"), payload
            assert payload.get("executed") is False
            path = Path(payload.get("artifact_path"))
            assert path.is_relative_to(context.session.outputs)
            assert f"temp.ads_parallel_{index}_ds" in path.read_text()
            assert f"temp.ads_parallel_{1 - index}_ds" not in path.read_text()
