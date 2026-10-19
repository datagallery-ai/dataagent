"""Exercise the deliverables quality gate through real DeepSeek streaming requests."""

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def real_llm():
    """Create a real DeepSeek adapter when explicitly enabled for end-to-end tests."""
    if os.getenv("RUN_DEEPSEEK_E2E") != "1":
        pytest.skip("Set RUN_DEEPSEEK_E2E=1 to run real model validation.")
    base_url = os.getenv("DEEPSEEK_BASE_URL")
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not base_url or not api_key:
        pytest.skip("DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY are required.")

    from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter
    from dataagent.core.managers.llm_manager.llm_client import LLMClient

    return LangChainChatModelAdapter(
        LLMClient(
            model=os.getenv("DEEPSEEK_MODEL", "DeepSeek-V4.1-Flash"),
            api_base=base_url,
            api_key=api_key,
            provider="deepseek",
            extra_body={"temperature": 0},
            timeout=180,
            num_retries=0,
        )
    )


@pytest.mark.parametrize("inside_event_loop", [False, True])
def test_deliverables_quality_gate_streaming(inside_event_loop, real_llm, monkeypatch, tmp_path):
    """Collect complete JSON in both sync caller contexts and retain LLM usage attribution."""
    from dataagent.actions.tools.local_tool.sandbox import (
        create_sandbox,
        get_current_sandbox,
        reset_current_sandbox,
        set_current_sandbox,
    )
    from dataagent.core.managers.llm_manager import llm_manager
    from dataagent.core.suite.builtin_suites.de_agent.tools import verify
    from dataagent.core.utils.performance import attribute_calls, bind_current_collector, create_collector

    ddl = tmp_path / "create_features.sql"
    ddl.write_text(
        "CREATE TABLE biads.ads_site_download_dm (siteid STRING COMMENT '榜单ID', "
        "download_cnt BIGINT COMMENT '下载量') COMMENT '榜单下载量' "
        "PARTITIONED BY (pt_d STRING COMMENT '日期') STORED AS ORC;",
        encoding="utf-8",
    )
    dml = tmp_path / "insert_features.sql"
    dml.write_text(
        "INSERT INTO TABLE biads.ads_site_download_dm PARTITION (pt_d='$date') "
        "SELECT siteid, download_cnt, pt_d FROM biads.source_download_dm WHERE pt_d='$date';",
        encoding="utf-8",
    )
    (tmp_path / "schema_schemair.md").write_text(
        "Hive: biads.source_download_dm(siteid STRING, download_cnt BIGINT, pt_d STRING partition). "
        "Rows are unique per siteid and pt_d.",
        encoding="utf-8",
    )
    llm = real_llm
    monkeypatch.setattr(llm_manager, "get_default_llm", lambda: llm)
    monkeypatch.setenv("DATAAGENT_PERFORMANCE_ENABLED", "1")
    collector = create_collector(workspace=tmp_path, run_id="quality_gate_streaming")
    sandbox = create_sandbox(enabled=False, workspace_root=tmp_path)
    chunks, parsed, request_modes, stream_finished = [], [], [], []
    original_send = httpx.AsyncClient.send
    original_stream = llm.astream
    original_parse = verify._parse_quality_gate_result

    async def record_request(client, request, **kwargs):
        """Observe the request flag while forwarding the real request unmodified."""
        request_modes.append(json.loads(request.content).get("stream", False))
        return await original_send(client, request, **kwargs)

    async def record_stream(prompts):
        """Record genuine chunks and verify context survives the sync-to-async bridge."""
        assert get_current_sandbox() is sandbox
        async for chunk in original_stream(prompts):
            chunks.append(chunk)
            yield chunk
        stream_finished.append(True)

    def record_parse(raw_result, warnings, include_suggestions=False):
        """Require parsing to happen after the complete stream, then use the real parser."""
        assert stream_finished == [True]
        parsed.append(json.loads(raw_result))
        original_parse(raw_result, warnings, include_suggestions)

    monkeypatch.setattr(httpx.AsyncClient, "send", record_request)
    monkeypatch.setattr(llm, "astream", record_stream)
    monkeypatch.setattr(verify, "_parse_quality_gate_result", record_parse)
    context = SimpleNamespace(runtime=SimpleNamespace(get_cache={}.get))
    warnings = []

    def run_gate():
        """Execute the production quality-gate entry with real SQL and model output."""
        verify._validate_deliverables_quality_gate(
            ddl, dml, warnings, "覆盖写入指定日期每个榜单的下载量，只输出榜单ID和下载量。", _tool_context=context
        )

    async def run_gate_in_loop():
        """Exercise a synchronous caller that already has a running event loop."""
        run_gate()

    sandbox_token = set_current_sandbox(sandbox)
    try:
        with bind_current_collector(collector), attribute_calls("tool", "validate_deliverables"):
            if inside_event_loop:
                asyncio.run(run_gate_in_loop())
            else:
                run_gate()
    finally:
        reset_current_sandbox(sandbox_token)
        collector.flush()

    assert request_modes == [True]
    assert len(chunks) > 1
    assert len(parsed) == 1
    final_response = chunks[-1].final_response
    assert final_response is not None
    assert parsed[0] == json.loads(final_response.content.strip())
    assert parsed[0].get("gate_passed") is False
    assert any("GATE-1" in item for item in parsed[0].get("gate_failures", []))
    assert any("GATE Check 未通过" in item for item in warnings)
    assert not any("执行异常" in item or "无效 JSON" in item for item in warnings)
    events = [event for event in collector.events if event.get("kind") == "llm"]
    assert len(events) == 1
    assert events[0].get("extra", {}).get("caller_name") == "validate_deliverables"
    assert events[0].get("extra", {}).get("call_mode") == "astream"
    assert final_response.usage_metadata.get("total_tokens", 0) > 0
    (tmp_path / "result.json").write_text(
        json.dumps(
            {
                "inside_event_loop": inside_event_loop,
                "stream_flags": request_modes,
                "chunks": len(chunks),
                "judgment": parsed[0],
                "warnings": warnings,
                "usage": final_response.usage_metadata,
                "performance": events,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("analysis_kind", ["count_zero", "error_log"])
def test_deliverables_dataops_analysis_streaming(analysis_kind, real_llm, monkeypatch, tmp_path):
    """Validate both downstream analysis paths with real streamed structured judgments."""
    from dataagent.actions.tools.local_tool import dataops_validate_tool

    request_modes, responses = [], []
    original_send = httpx.AsyncClient.send
    original_stream = real_llm.astream

    async def record_request(client, request, **kwargs):
        """Forward real requests while recording whether streaming was requested."""
        request_modes.append(json.loads(request.content).get("stream", False))
        return await original_send(client, request, **kwargs)

    async def record_stream(prompts):
        """Retain the normalized response for comparison with the parsed result."""
        async for chunk in original_stream(prompts):
            if chunk.final_response is not None:
                responses.append(chunk.final_response)
            yield chunk

    monkeypatch.setattr(httpx.AsyncClient, "send", record_request)
    monkeypatch.setattr(real_llm, "astream", record_stream)
    runtime = SimpleNamespace(llm=lambda name: real_llm)
    if analysis_kind == "count_zero":
        result = asyncio.run(
            dataops_validate_tool._analyze_count_zero_with_llm(
                "Insert every user's total downloads. Never exclude users with downloads.",
                "INSERT INTO user_downloads SELECT user_id, SUM(downloads) FROM downloads WHERE 1=0 GROUP BY user_id;",
                runtime=runtime,
            )
        )
        assert result.get("has_mismatch") is True
        assert result.get("fix_suggestion")
    else:
        result = asyncio.run(
            dataops_validate_tool._analyze_log_directly(
                None,
                "streaming-e2e",
                "ColumnNotFoundException: column missing_downloads not found",
                raw_log="AnalysisException: cannot resolve column 'missing_downloads' in downloads "
                "(user_id, downloads); line 1 pos 7",
                _tool_context=SimpleNamespace(runtime=runtime),
            )
        )
        assert result.get("status") == "ok"
        analysis = result.get("analysis", {})
        assert "missing_downloads" in analysis.get("location", {}).get("columns", [])
        assert analysis.get("suggestions")
    assert request_modes == [True]
    assert len(responses) == 1
    assert isinstance(json.loads(responses[0].content.strip()), dict)
    (tmp_path / "result.json").write_text(
        json.dumps(
            {"analysis_kind": analysis_kind, "stream_flags": request_modes, "result": result},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
