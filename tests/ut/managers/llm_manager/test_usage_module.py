# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for the shared canonical usage module (dataagent.core.managers.llm_manager.usage).

Covers:
- OpenAI/Qwen/DashScope inclusive ``input_tokens`` (cache inside input).
- Anthropic canonical ``input_tokens = raw_input + cache_read + cache_creation``.
- DeepSeek ``prompt_cache_hit_tokens`` fallback.
- ``total_tokens`` recomputation on the raw path.
- ``cache_hit_rate`` returning 0-1 decimal (None when input == 0).
- ``normalize_usage_metadata`` / ``summarize_usage`` not double-counting Anthropic input.
"""

from __future__ import annotations

from dataagent.core.managers.llm_manager.usage import (
    TOKEN_FIELDS,
    cache_hit_rate,
    normalize_usage_metadata,
    summarize_usage,
    usage_to_metadata,
)


class TestUsageToMetadataOpenAI:
    """OpenAI/Qwen/DashScope: prompt_tokens_details.cached_tokens; input inclusive."""

    def test_inclusive_input_with_cached_tokens(self):
        usage = {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 800, "cache_creation_tokens": 50},
            "completion_tokens_details": {"reasoning_tokens": 30},
        }
        meta = usage_to_metadata(usage)
        assert meta["input_tokens"] == 1000  # inclusive: unchanged
        assert meta["output_tokens"] == 200
        assert meta["total_tokens"] == 1200
        assert meta["input_cache_read_tokens"] == 800
        assert meta["input_cache_creation_tokens"] == 50
        assert meta["output_reasoning_tokens"] == 30

    def test_cache_creation_input_tokens_alias(self):
        usage = {
            "prompt_tokens": 500,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cache_creation_input_tokens": 7},
        }
        meta = usage_to_metadata(usage)
        assert meta["input_cache_creation_tokens"] == 7
        assert meta["input_tokens"] == 500


class TestUsageToMetadataAnthropic:
    """Anthropic: flat cache_read/creation; canonical input = raw + cache_read + cache_creation."""

    def test_canonical_input_correction(self):
        usage = {
            "input_tokens": 400,  # raw Anthropic input, does NOT include cache
            "output_tokens": 100,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 60,
            "reasoning_tokens": 25,
        }
        meta = usage_to_metadata(usage)
        # canonical input = 400 + 300 + 60 = 760
        assert meta["input_tokens"] == 760
        assert meta["output_tokens"] == 100
        assert meta["input_cache_read_tokens"] == 300
        assert meta["input_cache_creation_tokens"] == 60
        assert meta["output_reasoning_tokens"] == 25
        # total recomputed: 760 + 100 = 860 (raw total missing)
        assert meta["total_tokens"] == 860

    def test_anthropic_total_preserved_when_consistent(self):
        usage = {
            "input_tokens": 400,
            "output_tokens": 100,
            "total_tokens": 500,  # 400 + 100 (no cache correction needed for total? no: canonical input=400)
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        meta = usage_to_metadata(usage)
        # cache=0 → not anthropic_flat (cache_read and cache_creation both 0)
        # so input stays 400; total 500 == 400+100 → preserved
        assert meta["input_tokens"] == 400
        assert meta["total_tokens"] == 500


class TestUsageToMetadataDeepSeek:
    """DeepSeek: prompt_cache_hit_tokens fallback; input inclusive."""

    def test_prompt_cache_hit_tokens(self):
        usage = {
            "prompt_tokens": 900,
            "completion_tokens": 80,
            "total_tokens": 980,
            "prompt_cache_hit_tokens": 700,
        }
        meta = usage_to_metadata(usage)
        assert meta["input_tokens"] == 900  # inclusive
        assert meta["input_cache_read_tokens"] == 700
        assert meta["output_reasoning_tokens"] == 0

    def test_none_usage(self):
        meta = usage_to_metadata(None)
        assert all(meta[f] == 0 for f in TOKEN_FIELDS)


class TestCacheHitRate:
    def test_decimal_rate(self):
        assert cache_hit_rate({"input_tokens": 200, "input_cache_read_tokens": 150}) == 0.75

    def test_zero_input_returns_none(self):
        assert cache_hit_rate({"input_tokens": 0, "input_cache_read_tokens": 0}) is None

    def test_non_mapping_returns_none(self):
        assert cache_hit_rate(None) is None
        assert cache_hit_rate("not a dict") is None

    def test_no_cache_read(self):
        assert cache_hit_rate({"input_tokens": 100, "input_cache_read_tokens": 0}) == 0.0

    def test_anthropic_canonical_consistency(self):
        # Anthropic canonical input includes cache; rate should use canonical denominator.
        meta = usage_to_metadata(
            {"input_tokens": 400, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 60}
        )
        # canonical input = 760, cache_read = 300 → 300/760
        assert cache_hit_rate(meta) == round(300 / 760, 6)


class TestNormalizeNoDoubleCount:
    """normalize/summarize must NOT re-add cache to already-canonical Anthropic input."""

    def test_normalize_preserves_canonical_input(self):
        # Already-canonical usage (input includes cache, from usage_to_metadata).
        usage = {
            "input_tokens": 760,  # already canonical (raw 400 + cache 360)
            "output_tokens": 100,
            "total_tokens": 860,
            "input_cache_read_tokens": 300,
            "input_cache_creation_tokens": 60,
            "output_reasoning_tokens": 25,
        }
        norm = normalize_usage_metadata(usage)
        assert norm["input_tokens"] == 760  # not re-corrected
        assert norm["total_tokens"] == 860
        assert norm["input_cache_read_tokens"] == 300

    def test_normalize_renamed_anthropic_fields(self):
        # Half-normalized dict with Anthropic-style field names.
        usage = {
            "input_tokens": 760,
            "output_tokens": 100,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 60,
            "reasoning_tokens": 25,
        }
        norm = normalize_usage_metadata(usage)
        assert norm["input_cache_read_tokens"] == 300
        assert norm["input_cache_creation_tokens"] == 60
        assert norm["output_reasoning_tokens"] == 25
        assert norm["input_tokens"] == 760  # preserved, not re-corrected

    def test_summarize_equals_normalize(self):
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "input_cache_read_tokens": 3}
        assert summarize_usage(usage) == normalize_usage_metadata(usage)

    def test_normalize_none(self):
        norm = normalize_usage_metadata(None)
        assert all(norm[f] == 0 for f in TOKEN_FIELDS)


class TestResolveCacheControlMode:
    """LLMClient._resolve_cache_control_mode: explicit / implicit / none_or_unknown."""

    def test_explicit_when_supported(self):
        from dataagent.core.managers.llm_manager.llm_client import LLMClient

        client = LLMClient(
            model="qwen3.7-plus",
            api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="k",
            provider="bailian",
        )
        assert client._resolve_cache_control_mode({"input_cache_read_tokens": 0}) == "explicit"

    def test_implicit_when_not_injected_but_cache_usage(self):
        from dataagent.core.managers.llm_manager.llm_client import LLMClient

        client = LLMClient(
            model="deepseek-v4-flash",
            api_base="https://api.deepseek.com/v1",
            api_key="k",
            provider="deepseek",
        )
        assert (
            client._resolve_cache_control_mode({"input_cache_read_tokens": 500, "input_cache_creation_tokens": 0})
            == "implicit"
        )

    def test_none_or_unknown_when_no_cache_usage(self):
        from dataagent.core.managers.llm_manager.llm_client import LLMClient

        client = LLMClient(
            model="gpt-4o",
            api_base="https://api.openai.com/v1",
            api_key="k",
            provider="openai",
        )
        assert client._resolve_cache_control_mode({"input_cache_read_tokens": 0}) == "none_or_unknown"

    def test_wrap_response_sets_mode(self):
        from dataagent.core.managers.llm_manager.llm_client import LLMClient

        client = LLMClient(
            model="qwen3.7-plus",
            api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="k",
            provider="bailian",
        )
        resp = {
            "choices": [{"message": {"content": "hi", "tool_calls": []}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        msg = client._wrap_response(resp)
        assert msg.cache_control_mode == "explicit"
        assert msg.usage_metadata["input_tokens"] == 10


class TestSupportsExplicitCacheControlWhitelist:
    """Spec §4: Qwen injects only on whitelisted endpoints; generic endpoint strips."""

    def test_qwen_bailian_endpoint(self):
        from dataagent.core.managers.llm_manager.llm_client import _supports_explicit_cache_control

        assert _supports_explicit_cache_control("qwen-plus", provider="bailian") is True

    def test_qwen_generic_openai_endpoint_not_supported(self):
        from dataagent.core.managers.llm_manager.llm_client import _supports_explicit_cache_control

        assert _supports_explicit_cache_control("qwen-plus", provider="openai") is False
        assert _supports_explicit_cache_control("qwq-32b", provider="openai") is False

    def test_qwen_by_baseurl_hint(self):
        from dataagent.core.managers.llm_manager.llm_client import _supports_explicit_cache_control

        assert (
            _supports_explicit_cache_control(
                "qwen-plus", provider="openai", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
            )
            is True
        )

    def test_bailian_whitelist_models_require_bailian_endpoint(self):
        from dataagent.core.managers.llm_manager.llm_client import _supports_explicit_cache_control

        assert _supports_explicit_cache_control("deepseek-v3.2", provider="bailian") is True
        assert _supports_explicit_cache_control("deepseek-v3.2", provider="deepseek") is False
        assert _supports_explicit_cache_control("deepseek-v3.2") is False

    def test_claude_always_supported(self):
        from dataagent.core.managers.llm_manager.llm_client import _supports_explicit_cache_control

        assert _supports_explicit_cache_control("claude-3-5-sonnet") is True
        assert _supports_explicit_cache_control("claude-opus-4-8", provider="openai") is True

    def test_deepseek_direct_and_gpt_not_supported(self):
        from dataagent.core.managers.llm_manager.llm_client import _supports_explicit_cache_control

        assert _supports_explicit_cache_control("deepseek-chat", provider="deepseek") is False
        assert _supports_explicit_cache_control("gpt-4o", provider="openai") is False
