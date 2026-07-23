from __future__ import annotations

import pytest

from dataagent.core.managers.llm_manager.adapters import LangChainChatModelAdapter
from dataagent.core.managers.llm_manager.llm_client import (
    _MAX_BREAKPOINTS,
    LLMClient,
    _supports_explicit_cache_control,
)
from dataagent.core.managers.llm_manager.llm_config import LLMConfig
from dataagent.core.managers.llm_manager.llm_manager import LLMManager


def _make_config(**params_overrides) -> LLMConfig:
    params: dict = {"model": "deepseek-v3.2", "temperature": 0.0, **params_overrides}
    return LLMConfig(
        name="qwen3_coder",
        provider="qwen3_coder",
        model_type="chat",
        params=params,
    )


def test_llm_config_string_representations_do_not_expose_client_params() -> None:
    """repr/str must only expose safe LLM metadata."""
    config = _make_config(
        api_key="sk-sensitive-secret",
        base_url="https://user:password@internal.example/v1",
        headers={"Authorization": "Bearer private-token"},
    )

    expected = "LLMConfig(name='qwen3_coder', provider='qwen3_coder', model_type='chat', section='qwen3_coder')"
    assert repr(config) == expected
    assert str(config) == expected
    assert "sk-sensitive-secret" not in repr(config)
    assert "private-token" not in repr(config)
    assert "internal.example" not in repr(config)

    assert config.client_params()["api_key"] == "sk-sensitive-secret"
    assert config.to_dict()["api_key"] == "sk-sensitive-secret"


def test_from_llm_config_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")

    client = LLMClient.from_llm_config(_make_config(enable_thinking=True))

    assert isinstance(client, LLMClient)
    assert client._model == "deepseek-v3.2"
    assert client._api_base == "https://example.invalid/v1"
    assert client._api_key == "sk-test"
    assert client._extra_body.get("enable_thinking") is True
    assert client._extra_body.get("temperature") == 0.0
    assert "custom_llm_provider" not in client._extra_body


def test_from_llm_config_params_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """params 中的 base_url / api_key 优先于 env。"""
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://from-env/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-env")

    client = LLMClient.from_llm_config(_make_config(base_url="https://from-yaml/v1", api_key="sk-yaml"))

    assert client._api_base == "https://from-yaml/v1"
    assert client._api_key == "sk-yaml"
    assert client._extra_body.get("temperature") == 0.0
    assert "base_url" not in client._extra_body
    assert "api_key" not in client._extra_body


def test_from_llm_config_reads_params_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置 .env 时，可从 YAML params 读取 base_url / api_key（quickstart 场景）。"""
    monkeypatch.delenv("QWEN3_CODER_BASE_URL", raising=False)
    monkeypatch.delenv("QWEN3_CODER_API_KEY", raising=False)

    client = LLMClient.from_llm_config(_make_config(base_url="https://from-yaml/v1", api_key="sk-yaml"))

    assert client._api_base == "https://from-yaml/v1"
    assert client._api_key == "sk-yaml"


def test_from_llm_config_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")

    config = LLMConfig(
        name="qwen3_coder",
        provider="qwen3_coder",
        model_type="chat",
        params={},
    )
    with pytest.raises(ValueError, match="Missing model"):
        LLMClient.from_llm_config(config)


def test_from_llm_config_missing_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QWEN3_CODER_BASE_URL", raising=False)
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")
    with pytest.raises(ValueError, match="BASE_URL"):
        LLMClient.from_llm_config(_make_config())


def test_from_llm_config_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.delenv("QWEN3_CODER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        LLMClient.from_llm_config(_make_config())


def test_llm_manager_create_llm_uses_llm_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMManager.create_llm 底层应为 LLMClient（经 LangChainChatModelAdapter 包装）。"""
    monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")

    manager = LLMManager()
    adapter = manager.create_llm(_make_config())

    assert isinstance(adapter, LangChainChatModelAdapter)
    assert isinstance(adapter.raw, LLMClient)


def test_llm_manager_create_llm_embedding_registers_config_only() -> None:
    """embedding 模型只缓存配置，不构造 LLMClient。"""
    manager = LLMManager()
    config = LLMConfig(
        name="jina_v3",
        provider="jina",
        model_type="embedding",
        params={"model": "jina-embeddings-v3"},
    )

    instance = manager.create_llm(config)

    assert instance is None
    assert manager.get_llm("jina_v3") is None
    assert manager.get_llm_config("jina_v3") is config


def _make_qwen_config(**params_overrides) -> LLMConfig:
    params: dict = {"model": "Qwen3.6-Plus", **params_overrides}
    return LLMConfig(
        name="qwen_plus",
        provider="qwen_plus",
        model_type="chat",
        params=params,
    )


class TestSupportsExplicitCacheControl:
    def test_qwen_prefix(self):
        assert _supports_explicit_cache_control("qwen-plus") is True
        assert _supports_explicit_cache_control("Qwen3.6-Plus") is True
        assert _supports_explicit_cache_control("QWEN-MAX") is True

    def test_qwq_prefix(self):
        assert _supports_explicit_cache_control("qwq-32b") is True
        assert _supports_explicit_cache_control("QwQ-plus") is True

    def test_dashscope_prefix(self):
        assert _supports_explicit_cache_control("dashscope/qwen-plus") is True

    def test_claude(self):
        assert _supports_explicit_cache_control("claude-3-5-sonnet") is True
        assert _supports_explicit_cache_control("claude-opus-4-8") is True
        assert _supports_explicit_cache_control("Claude-Sonnet-4.5") is True

    def test_claude_by_provider(self):
        assert _supports_explicit_cache_control("some-model", provider="anthropic") is True

    def test_bailian_non_qwen(self):
        assert _supports_explicit_cache_control("deepseek-v3.2") is True
        assert _supports_explicit_cache_control("kimi-k2.6") is True
        assert _supports_explicit_cache_control("glm-5.1") is True

    def test_bailian_deepseek_v4_not_supported(self):
        assert _supports_explicit_cache_control("deepseek-v4-flash") is False
        assert _supports_explicit_cache_control("deepseek-v4-pro") is False

    def test_non_supported(self):
        assert _supports_explicit_cache_control("deepseek-chat") is False
        assert _supports_explicit_cache_control("gpt-4o") is False
        assert _supports_explicit_cache_control("llama-3.1-70b") is False


class TestQwenCacheDefaults:
    """自实现客户端不再使用 litellm 的 custom_llm_provider 参数。

    验证：即使配置中显式传入 custom_llm_provider，也不会进入 extra_body（避免误传给 API）。
    """

    def test_from_llm_config_strips_custom_llm_provider(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("QWEN_PLUS_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        monkeypatch.setenv("QWEN_PLUS_API_KEY", "sk-test")

        client = LLMClient.from_llm_config(_make_qwen_config(custom_llm_provider="dashscope"))

        assert client._model == "Qwen3.6-Plus"
        assert "custom_llm_provider" not in client._extra_body

    def test_from_env_cfg_strips_custom_llm_provider(self):
        cfg = {
            "model": "Qwen3.6-Plus",
            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-test",
            "custom_llm_provider": "openai",
        }
        client = LLMClient.from_env_cfg(cfg)

        assert "custom_llm_provider" not in client._extra_body

    def test_from_env_cfg_keeps_provider_for_cache_check(self):
        cfg = {
            "model": "Qwen3.6-Plus",
            "api_base": "https://example.invalid/v1",
            "api_key": "sk-test",
            "provider": "dashscope",
        }
        client = LLMClient.from_env_cfg(cfg)

        assert client._provider == "dashscope"


class TestDashscopeCacheControl:
    """Tests for _apply_cache_control_with_anchors bp allocation.

    去除 StableUser/VariableUser split 后，主 Agent 无 explicit cc user 消息；
    bp1=history_summary, bp2=动态 first-large-tool/tail2, bp3=tail_anchor。
    """

    def test_empty_system_not_transformed(self):
        msgs = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "Hello"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        assert result[0]["content"] == ""

    def test_explicit_cache_control_respected(self):
        """Messages with pre-existing cc are not double-cc'd."""
        msgs = [
            {
                "role": "system",
                "content": [{"type": "text", "text": "x" * 4000, "cache_control": {"type": "ephemeral"}}],
            },
            {"role": "user", "content": [{"type": "text", "text": "y" * 4000, "cache_control": {"type": "ephemeral"}}]},
            {"role": "user", "content": "short variable"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        assert result[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert result[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert result[2]["content"] == "short variable"


class TestHistorySummaryBp1:
    """Tests for bp0 (System) + bp1 (history_summary)."""

    @staticmethod
    def _has_cc(msg: dict) -> bool:
        c = msg.get("content")
        if isinstance(c, list):
            return any(isinstance(p, dict) and "cache_control" in p for p in c)
        return False

    def test_bp0_system_always_gets_cc(self):
        """bp0 = System: non-empty System always gets cc in all scenarios."""
        msgs = [
            {"role": "system", "content": "System prompt content"},
            {"role": "user", "content": "User query"},
            {"role": "user", "content": "# Work Plan Status\nDone"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        assert self._has_cc(result[0]), "bp0 (System) should always have cc"

    def test_bp0_empty_system_no_cc(self):
        """bp0 = System: empty System content should not get cc."""
        msgs = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "User query"},
            {"role": "user", "content": "# Work Plan Status\nDone"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        assert not self._has_cc(result[0]), "empty System should NOT have cc"

    def test_bp0_survives_compression_scenario(self):
        """bp0 + bp1 both set when history_summary present (post-compression scenario)."""
        msgs = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "User query"},
            {
                "role": "user",
                "content": "<history_summary>\nIntent\n</history_summary>",
                "additional_kwargs": {"_folded": True},
            },
            {"role": "user", "content": "<user_query>Q</user_query>"},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": "result"},
            {"role": "user", "content": "# Work Plan Status\nDone"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        assert self._has_cc(result[0]), "bp0 (System) should have cc"
        assert self._has_cc(result[2]), "bp1 (history_summary) should have cc"

    def test_history_summary_folded_marker_sets_bp1(self):
        """history_summary with _folded=True → bp1; user [1] is plain (no cc)."""
        msgs = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "User query and context"},
            {
                "role": "user",
                "content": "<history_summary>\nIntent\n</history_summary>",
                "additional_kwargs": {"_folded": True},
            },
            {"role": "user", "content": "<user_query>Q</user_query>"},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": "result"},
            {"role": "user", "content": "# Work Plan Status\nDone"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        # bp1 = history_summary [2]
        assert self._has_cc(result[2]), "bp1 (history_summary) should have cc"
        # user [1] is a plain message — no explicit cc (split removed)
        assert not self._has_cc(result[1]), "user [1] should NOT have cc (no StableUser)"
        # bp3 = tail_anchor [5] (todo_idx-1 = 6-1 = 5)
        assert self._has_cc(result[5]), "bp3 (tail_anchor) should have cc"

    def test_history_summary_content_prefix_fallback(self):
        """If _folded marker missing, detect by <history_summary> content prefix."""
        msgs = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "User query and context"},
            {"role": "user", "content": "<history_summary>\nIntent\n</history_summary>"},
            {"role": "user", "content": "# Work Plan Status\nDone"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        assert self._has_cc(result[2]), "bp1 should be on history_summary [2] (content prefix fallback)"

    def test_no_history_summary_no_bp1(self):
        """No history_summary → bp1=None; bp3=tail still has cc.

        bp2 falls back to tail2 (todo_idx-3) because no qualifying first-large-tool
        exists (tool content "T"*600 yields spacing < _MIN_SPACING_CHARS).
        """
        msgs = [
            {"role": "system", "content": "S" * 3000},
            {"role": "user", "content": "User query and context"},
            {"role": "user", "content": "<user_query>Q</user_query>"},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": "T" * 600},
            {"role": "user", "content": "# Work Plan Status\nDone"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        # bp1 = None (no history_summary); bp2 = tail2 fallback [2]
        assert self._has_cc(result[2]), "bp2 (tail2 fallback) should have cc"
        # bp3 = tail [4] (todo_idx-1 = 5-1 = 4)
        assert self._has_cc(result[4]), "bp3 (tail) should have cc"

    def test_bp3_not_skipped_by_approaching_compress(self):
        """bp3 must be set even when approaching_compress — the gap between
        0.8×threshold and actual compression can span 10+ calls where bp3
        provides critical within-iteration hits."""
        msgs = [
            {"role": "system", "content": "S" * 100},
            {"role": "user", "content": "User query and context"},
            {
                "role": "user",
                "content": "<history_summary>\nI\n</history_summary>",
                "additional_kwargs": {"_folded": True},
            },
        ]
        # DEFAULT_COMPRESS_MESSAGE_CNT=200, approaching at 0.8*200=160 messages
        for i in range(80):
            msgs.append({"role": "assistant", "content": f"AI_{i}"})
            msgs.append({"role": "tool", "content": f"R_{i}"})
        msgs.append({"role": "user", "content": "# Work Plan Status\nDone"})
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        tail_idx = len(msgs) - 2
        assert self._has_cc(result[tail_idx]), "bp3 must NOT be skipped by approaching_compress"

    def test_bp4_skipped_by_approaching_compress(self):
        """bp4 (tail2) is skipped when approaching_compress — tail2 cache
        is wasted if compression triggers soon after."""
        msgs = [
            {"role": "system", "content": "S" * 100},
            {"role": "user", "content": "User query and context"},
            {
                "role": "user",
                "content": "<history_summary>\nI\n</history_summary>",
                "additional_kwargs": {"_folded": True},
            },
        ]
        # DEFAULT_COMPRESS_MESSAGE_CNT=200, approaching at 0.8*200=160 messages
        for i in range(80):
            msgs.append({"role": "assistant", "content": f"AI_{i}"})
            msgs.append({"role": "tool", "content": f"R_{i}"})
        msgs.append({"role": "user", "content": "# Work Plan Status\nDone"})
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        # bp4 = tail2 = todo_idx - 3
        tail2_idx = len(msgs) - 4
        assert not self._has_cc(result[tail2_idx]), "bp4 should be skipped when approaching_compress"

    def test_env_var_disables_tail_anchors(self, monkeypatch):
        """DATAAGENT_CACHE_ANCHOR=0 disables bp2/bp3/bp4 (bp0/bp1 survive)."""
        monkeypatch.setenv("DATAAGENT_CACHE_ANCHOR", "0")
        msgs = [
            {"role": "system", "content": "S" * 3000},
            {"role": "user", "content": "User query and context"},
            {"role": "user", "content": "<user_query>Q</user_query>"},
            {"role": "assistant", "content": ""},
            {"role": "tool", "content": "T" * 600},
            {"role": "user", "content": "# Work Plan Status\nDone"},
        ]
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        # bp3 (tail [4]) should NOT have cc when env var is "0"
        assert not self._has_cc(result[4])

    def test_max_breakpoints_enforced(self):
        long_body = "x" * 5000
        msgs = [
            {"role": "system", "content": long_body},
            {"role": "user", "content": long_body},
        ]
        for _i in range(10):
            msgs.append({"role": "assistant", "content": long_body})
            msgs.append({"role": "tool", "content": long_body})
        msgs.append({"role": "user", "content": "# Work Plan Status\nDone"})
        result = LLMClient._apply_cache_control_with_anchors(msgs)
        cc_count = 0
        for m in result:
            content = m.get("content")
            if isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and "cache_control" in p:
                        cc_count += 1
        assert cc_count <= _MAX_BREAKPOINTS


class TestDumpPromptAnnotatesAllBreakpoints:
    """dump_prompt_to_file must annotate ALL breakpoints (including dynamically
    injected bp2/bp3/bp4), not just the pre-existing explicit cc (bp1).

    Regression guard: previously the dump only scanned LangChain messages for
    pre-existing cache_control, missing the dynamic breakpoints that
    _apply_cache_control_with_anchors adds at LLM call time.
    """

    @staticmethod
    def _build_round_like_messages():
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

        return [
            SystemMessage(content="# Role\nYou are a DataAgent." + " x" * 200),
            HumanMessage(content="StableUser constraints"),
            HumanMessage(content="# User Query\n<user_query>test</user_query>"),
            AIMessage(content="", tool_calls=[{"id": "c1", "name": "get_tool", "args": {}}]),
            ToolMessage(tool_call_id="c1", content="<results>\n" + "ontology data\n" * 500 + "</results>"),
            ToolMessage(tool_call_id="c2", content="<results>skill md</results>"),
            AIMessage(content="ok", tool_calls=[{"id": "c3", "name": "nl2sql", "args": {}}]),
            ToolMessage(tool_call_id="c3", content="<results>SQL done 1</results>"),
            ToolMessage(tool_call_id="c4", content="<results>SQL done 2</results>"),
            ToolMessage(tool_call_id="c5", content="<results>SQL done 3</results>"),
            AIMessage(content="", tool_calls=[{"id": "c6", "name": "read_file", "args": {}}]),
            ToolMessage(tool_call_id="c6", content="<results>\nid,cell_type\n800001,huh-7\n</results>"),
            ToolMessage(tool_call_id="c7", content="<results>\nid,concentration\n11396,1.5\n</results>"),
            ToolMessage(tool_call_id="c8", content="<results>\nid,virus_type\n</results>"),
            HumanMessage(content="# Work Plan Status\nno plan"),
        ]

    def test_dump_annotates_dynamic_breakpoints(self, tmp_path):
        from dataagent.utils.messages_utils import dump_prompt_to_file

        msgs = self._build_round_like_messages()
        out = tmp_path / "round_3.txt"
        dump_prompt_to_file(msgs, out, annotate_cache_breakpoints=True)
        text = out.read_text(encoding="utf-8")

        # bp tags in headers
        bp_tags = [line for line in text.splitlines() if "[bp " in line and "---" in line]
        assert len(bp_tags) >= 3, (
            f"expected >=3 annotated breakpoints (bp0 System + dynamic bp2/bp3/bp4), "
            f"got {len(bp_tags)}:\n{chr(10).join(bp_tags)}"
        )

        # dynamic markers for breakpoints injected by _apply_cache_control_with_anchors
        assert "cache_control (dynamic)" in text, "dynamic breakpoints (bp0/bp2/bp3/bp4) must be marked"

        # bp0 (System) should be annotated as bp 1 in the header
        assert "[0] SYSTEM [bp 1 cc]" in text, "bp0 (System) should be annotated as bp 1"

        # summary line reflects final allocation count
        summary_lines = [line for line in text.splitlines() if "Breakpoint summary" in line]
        assert summary_lines, "missing breakpoint summary footer"
        assert "final allocation" in summary_lines[0]

    def test_dump_bp_count_matches_anchor_allocation(self, tmp_path):
        from dataagent.utils.messages_utils import dump_prompt_to_file

        msgs = self._build_round_like_messages()

        # ground truth: run the anchor function directly on dict form
        dict_msgs = LangChainChatModelAdapter.messages_to_openai_dicts(msgs)
        processed = LLMClient._apply_cache_control_with_anchors(dict_msgs)
        expected_bp = sum(
            1
            for m in processed
            if isinstance(m.get("content"), list)
            and any(isinstance(p, dict) and "cache_control" in p for p in m["content"])
        )

        out = tmp_path / "round_3.txt"
        dump_prompt_to_file(msgs, out, annotate_cache_breakpoints=True)
        text = out.read_text(encoding="utf-8")

        bp_tags = [line for line in text.splitlines() if "[bp " in line and "---" in line]
        assert len(bp_tags) == expected_bp, (
            f"dump bp count ({len(bp_tags)}) must match anchor allocation ({expected_bp})"
        )

    def test_dump_passes_compress_config_to_anchor(self, tmp_path):
        """With a low compress_message_cnt, approaching_compress triggers and bp4
        is skipped — the dump must reflect this when compress config is passed."""
        from dataagent.utils.messages_utils import dump_prompt_to_file

        msgs = self._build_round_like_messages()
        out = tmp_path / "round_3.txt"
        # compress_message_cnt=3 → approaching at 0.8*3=2.4, i.e. almost always True
        dump_prompt_to_file(msgs, out, annotate_cache_breakpoints=True, compress_message_cnt=3)
        text = out.read_text(encoding="utf-8")

        # ground truth with the same compress config
        dict_msgs = LangChainChatModelAdapter.messages_to_openai_dicts(msgs)
        processed = LLMClient._apply_cache_control_with_anchors(dict_msgs, compress_message_cnt=3)
        expected_bp = sum(
            1
            for m in processed
            if isinstance(m.get("content"), list)
            and any(isinstance(p, dict) and "cache_control" in p for p in m["content"])
        )
        bp_tags = [line for line in text.splitlines() if "[bp " in line and "---" in line]
        assert len(bp_tags) == expected_bp


class TestEnableCacheControlSwitch:
    """验证 ``enable_cache_control`` 三层控制（env var > YAML per-LLM > auto-detect）。

    见 ``docs/main_agent_cache_optimization_design.md`` §2.5.1。
    """

    @staticmethod
    def _make_client(
        model: str = "Qwen3.7-Plus",
        *,
        enable_cache_control: bool | None = None,
        provider: str | None = None,
    ) -> LLMClient:
        """构造测试用 LLMClient（绕过 from_env_cfg 的 pop 逻辑，直接传 kwarg）。"""
        return LLMClient(
            model=model,
            api_base="https://example.invalid/v1",
            api_key="sk-test",
            provider=provider,
            enable_cache_control=enable_cache_control,
        )

    def test_env_var_disables_cc(self, monkeypatch: pytest.MonkeyPatch):
        """L1: DATAAGENT_CACHE_CONTROL=0 全局禁用，即使 Qwen 模型也不注入 cc。"""
        monkeypatch.setenv("DATAAGENT_CACHE_CONTROL", "0")
        client = self._make_client(model="Qwen3.7-Plus")  # auto-detect 会返回 True
        assert client._should_inject_cache_control() is False

    def test_yaml_explicit_false_disables_cc(self, monkeypatch: pytest.MonkeyPatch):
        """L2: enable_cache_control=False 强制禁用，即使 Qwen 模型也不注入 cc。"""
        monkeypatch.delenv("DATAAGENT_CACHE_CONTROL", raising=False)
        client = self._make_client(model="Qwen3.7-Plus", enable_cache_control=False)
        assert client._should_inject_cache_control() is False

    def test_yaml_none_uses_auto_detect(self, monkeypatch: pytest.MonkeyPatch):
        """L3: enable_cache_control=None 走自动探测（向后兼容）。"""
        monkeypatch.delenv("DATAAGENT_CACHE_CONTROL", raising=False)
        client_qwen = self._make_client(model="Qwen3.7-Plus", enable_cache_control=None)
        assert client_qwen._should_inject_cache_control() is True  # Qwen supports cc

        client_deepseek = self._make_client(model="deepseek-v4-flash", enable_cache_control=None)
        assert client_deepseek._should_inject_cache_control() is False  # not in support list

    def test_yaml_explicit_true_forces_enable(self, monkeypatch: pytest.MonkeyPatch):
        """L2: enable_cache_control=True 强制启用，即使模型不在支持列表。"""
        monkeypatch.delenv("DATAAGENT_CACHE_CONTROL", raising=False)
        # deepseek-v4-flash 不支持显式缓存（auto-detect=False），但 True 强制开
        client = self._make_client(model="deepseek-v4-flash", enable_cache_control=True)
        assert client._should_inject_cache_control() is True

    def test_env_var_overrides_yaml_true(self, monkeypatch: pytest.MonkeyPatch):
        """L1 优先级 > L2: env=0 时，即使 YAML enable_cache_control=True 也禁用。"""
        monkeypatch.setenv("DATAAGENT_CACHE_CONTROL", "0")
        client = self._make_client(model="Qwen3.7-Plus", enable_cache_control=True)
        assert client._should_inject_cache_control() is False

    def test_from_env_cfg_pops_enable_cc(self):
        """from_env_cfg 不把 enable_cache_control 泄漏到 extra_body。"""
        cfg = {
            "model": "Qwen3.7-Plus",
            "api_base": "https://example.invalid/v1",
            "api_key": "sk-test",
            "enable_cache_control": False,
        }
        client = LLMClient.from_env_cfg(cfg)
        assert client._enable_cache_control is False
        assert "enable_cache_control" not in client._extra_body

    def test_from_llm_config_pops_enable_cc(self, monkeypatch: pytest.MonkeyPatch):
        """from_llm_config 不把 enable_cache_control 泄漏到 extra_body。"""
        monkeypatch.setenv("QWEN3_CODER_BASE_URL", "https://example.invalid/v1")
        monkeypatch.setenv("QWEN3_CODER_API_KEY", "sk-test")
        config = _make_config(enable_cache_control=False)
        client = LLMClient.from_llm_config(config)
        assert client._enable_cache_control is False
        assert "enable_cache_control" not in client._extra_body

    def test_bind_tools_preserves_enable_cache_control(self):
        """bind_tools 透传 enable_cache_control 到新实例。"""
        client = self._make_client(model="Qwen3.7-Plus", enable_cache_control=False)
        bound = client.bind_tools([])
        assert bound._enable_cache_control is False
        assert bound._should_inject_cache_control() is False

    def test_dump_no_bp_when_enable_cache_control_false(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        """enable_cache_control=False 时 dump 不标注断点，header 显示 OFF。"""
        monkeypatch.delenv("DATAAGENT_CACHE_CONTROL", raising=False)
        from langchain_core.messages import HumanMessage, SystemMessage

        from dataagent.utils.messages_utils import dump_prompt_to_file

        msgs = [
            SystemMessage(content="System prompt"),
            HumanMessage(content="User query"),
            HumanMessage(content="# Work Plan Status\nDone"),
        ]
        out = tmp_path / "round_test.txt"
        dump_prompt_to_file(
            msgs,
            out,
            annotate_cache_breakpoints=True,
            enable_cache_control=False,
        )
        text = out.read_text(encoding="utf-8")
        assert "Cache Breakpoint Annotation: OFF" in text, "dump header should show OFF when enable_cache_control=False"
        assert "[bp " not in text, "no bp tags should appear when cache_control disabled"

    def test_dump_no_bp_when_env_var_disables(self, tmp_path, monkeypatch: pytest.MonkeyPatch):
        """DATAAGENT_CACHE_CONTROL=0 时 dump 不标注断点。"""
        monkeypatch.setenv("DATAAGENT_CACHE_CONTROL", "0")
        from langchain_core.messages import HumanMessage, SystemMessage

        from dataagent.utils.messages_utils import dump_prompt_to_file

        msgs = [
            SystemMessage(content="System prompt"),
            HumanMessage(content="User query"),
            HumanMessage(content="# Work Plan Status\nDone"),
        ]
        out = tmp_path / "round_test.txt"
        dump_prompt_to_file(msgs, out, annotate_cache_breakpoints=True)
        text = out.read_text(encoding="utf-8")
        assert "Cache Breakpoint Annotation: OFF" in text
        assert "[bp " not in text
