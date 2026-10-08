"""Verify NL2SQL streaming against a real DeepSeek-compatible model and SQLite."""

import json
import os
import sqlite3

import httpx
import pytest


@pytest.mark.asyncio
async def test_nl2sql_streaming_sql_and_json(monkeypatch):
    """Require streamed HTTP requests and validate complete SQL and JSON node outputs."""
    if os.getenv("RUN_DEEPSEEK_E2E") != "1":
        pytest.skip("Set RUN_DEEPSEEK_E2E=1 to run real model validation.")
    base_url = os.getenv("DEEPSEEK_BASE_URL")
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not base_url or not api_key:
        pytest.skip("DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY are required.")

    from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
    from dataagent.agents.nl2sql.nodes.generator import GeneratorNode
    from dataagent.core.managers.llm_manager import llm_manager
    from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter
    from dataagent.core.managers.llm_manager.llm_client import LLMClient

    requests: list[dict] = []

    original_send = httpx.AsyncClient.send

    async def record_request(client: httpx.AsyncClient, request: httpx.Request, **kwargs) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload.get("stream") is True
        requests.append(payload)
        return await original_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", record_request)
    raw = LLMClient(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        api_base=base_url,
        api_key=api_key,
        extra_body={"temperature": 0},
        timeout=120,
        provider="deepseek",
        num_retries=0,
    )
    llm = LangChainChatModelAdapter(raw)
    monkeypatch.setattr(llm_manager, "get_default_llm", lambda: llm)
    context = {
        "question": "How many employees are there? Return only the count.",
        "schema": "CREATE TABLE employees (id INTEGER, name TEXT);",
        "evidence": "",
        "few_shot_examples": "",
    }
    candidates = await GeneratorNode().generate_with_llm(
        "prompt", {"dialect": "sqlite", "num_samples": 1, "sql_rules": ""}, context
    )
    assert len(candidates) == 1
    sql, _, _ = candidates[0]
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE employees (id INTEGER, name TEXT)")
        db.executemany("INSERT INTO employees VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
        assert db.execute(sql).fetchall() == [(2,)]
    scores = await BaseNL2SQLNode(name="selector").execute_with_llm_json(
        {**context, "sql_rules": "", "res": "SQL id=0; columns: count; rows: [(2,)]; error: none"}
    )
    assert isinstance(scores, list) and scores
    assert scores[0].get("id") == 0
    assert scores[0].get("score", 0) >= 0.9
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_real_model_inline_reasoning(stream, monkeypatch):
    """Validate marked output through real streaming and non-streaming model requests."""
    if os.getenv("RUN_DEEPSEEK_E2E") != "1":
        pytest.skip("Set RUN_DEEPSEEK_E2E=1 to run real model validation.")
    base_url = os.getenv("DEEPSEEK_BASE_URL")
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not base_url or not api_key:
        pytest.skip("DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY are required.")

    from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter
    from dataagent.core.managers.llm_manager.llm_client import LLMClient

    original_send = httpx.AsyncClient.send
    request_modes: list[bool] = []

    async def record_request(client: httpx.AsyncClient, request: httpx.Request, **kwargs) -> httpx.Response:
        request_modes.append(json.loads(request.content).get("stream", False))
        return await original_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", record_request)
    llm = LangChainChatModelAdapter(
        LLMClient(
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            api_base=base_url,
            api_key=api_key,
            extra_body={"temperature": 0},
            timeout=120,
            provider="deepseek",
            num_retries=0,
        )
    )
    prompts = [
        {
            "role": "user",
            "content": "Copy exactly this literal text, without quotes or code fences: internal notes</think>SELECT 1",
        }
    ]
    if stream:
        chunks = [chunk async for chunk in llm.astream(prompts)]
        response = chunks[-1].final_response
        assert response is not None
    else:
        response = await llm.ainvoke(prompts)
    assert response.content == "SELECT 1"
    assert "internal notes" in response.reasoning_content
    assert request_modes == [stream]
