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
"""Unit tests for LLM cache/reasoning token extraction from different provider formats.

Covers:
- OpenAI/DeepSeek format (nested in ``prompt_tokens_details`` / ``completion_tokens_details``)
- Anthropic format (flat fields directly on ``usage``)
- Dict-based extraction for stream-chunk path
- ``normalize_usage_metadata`` producing 6 required fields
"""

from __future__ import annotations

from dataagent.core.managers.llm_manager.adapters import normalize_usage_metadata
from dataagent.core.managers.llm_manager.llm_client import (
    _extract_detail_tokens,
    _extract_detail_tokens_from_dict,
)


class _MockUsage:
    """Helper to build a mock litellm ``usage`` object."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestExtractDetailTokensOpenAI:
    """OpenAI/DeepSeek uses nested prompt_tokens_details / completion_tokens_details."""

    def _make_usage(self):
        return _MockUsage(
            prompt_tokens_details=_MockUsage(cached_tokens=80, cache_creation_tokens=20),
            completion_tokens_details=_MockUsage(reasoning_tokens=30),
        )

    def test_cache_read(self):
        target = {}
        _extract_detail_tokens(self._make_usage(), target)
        assert target["input_cache_read_tokens"] == 80

    def test_cache_creation(self):
        target = {}
        _extract_detail_tokens(self._make_usage(), target)
        assert target["input_cache_creation_tokens"] == 20

    def test_reasoning(self):
        target = {}
        _extract_detail_tokens(self._make_usage(), target)
        assert target["output_reasoning_tokens"] == 30


class TestExtractDetailTokensAnthropic:
    """Anthropic returns flat fields directly on the usage object."""

    def _make_usage(self):
        return _MockUsage(
            cache_read_input_tokens=70,
            cache_creation_input_tokens=15,
            reasoning_tokens=25,
        )

    def test_cache_read(self):
        target = {}
        _extract_detail_tokens(self._make_usage(), target)
        assert target["input_cache_read_tokens"] == 70

    def test_cache_creation(self):
        target = {}
        _extract_detail_tokens(self._make_usage(), target)
        assert target["input_cache_creation_tokens"] == 15

    def test_reasoning(self):
        target = {}
        _extract_detail_tokens(self._make_usage(), target)
        assert target["output_reasoning_tokens"] == 25


class TestExtractDetailTokensNoneFields:
    """When detailed fields are absent, all values default to 0."""

    def test_openai_missing(self):
        usage = _MockUsage()
        target = {}
        _extract_detail_tokens(usage, target)
        assert target["input_cache_read_tokens"] == 0
        assert target["input_cache_creation_tokens"] == 0
        assert target["output_reasoning_tokens"] == 0

    def test_anthropic_missing(self):
        usage = _MockUsage(
            prompt_tokens_details=None,
            completion_tokens_details=None,
        )
        target = {}
        _extract_detail_tokens(usage, target)
        assert target["input_cache_read_tokens"] == 0
        assert target["input_cache_creation_tokens"] == 0
        assert target["output_reasoning_tokens"] == 0


class TestExtractDetailTokensFromDictOpenAI:
    """Dict-based extraction (stream chunks) for OpenAI/DeepSeek format."""

    def _make_usage_dict(self):
        return {
            "prompt_tokens_details": {"cached_tokens": 60, "cache_creation_tokens": 10},
            "completion_tokens_details": {"reasoning_tokens": 40},
        }

    def test_cache_read(self):
        target = {}
        _extract_detail_tokens_from_dict(self._make_usage_dict(), target)
        assert target["input_cache_read_tokens"] == 60

    def test_cache_creation(self):
        target = {}
        _extract_detail_tokens_from_dict(self._make_usage_dict(), target)
        assert target["input_cache_creation_tokens"] == 10

    def test_reasoning(self):
        target = {}
        _extract_detail_tokens_from_dict(self._make_usage_dict(), target)
        assert target["output_reasoning_tokens"] == 40


class TestExtractDetailTokensFromDictAnthropic:
    """Dict-based extraction (stream chunks) for Anthropic format."""

    def _make_usage_dict(self):
        return {
            "cache_read_input_tokens": 55,
            "cache_creation_input_tokens": 15,
            "reasoning_tokens": 35,
        }

    def test_cache_read(self):
        target = {}
        _extract_detail_tokens_from_dict(self._make_usage_dict(), target)
        assert target["input_cache_read_tokens"] == 55

    def test_cache_creation(self):
        target = {}
        _extract_detail_tokens_from_dict(self._make_usage_dict(), target)
        assert target["input_cache_creation_tokens"] == 15

    def test_reasoning(self):
        target = {}
        _extract_detail_tokens_from_dict(self._make_usage_dict(), target)
        assert target["output_reasoning_tokens"] == 35


class TestNormalizeUsageMetadata:
    """normalize_usage_metadata should always produce all 6 required fields."""

    def test_all_six_fields_present(self):
        um = normalize_usage_metadata({
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "input_cache_read_tokens": 80,
            "input_cache_creation_tokens": 20,
            "output_reasoning_tokens": 30,
        })
        assert um["input_tokens"] == 100
        assert um["output_tokens"] == 50
        assert um["total_tokens"] == 150
        assert um["input_cache_read_tokens"] == 80
        assert um["input_cache_creation_tokens"] == 20
        assert um["output_reasoning_tokens"] == 30

    def test_missing_cache_defaults_to_zero(self):
        um = normalize_usage_metadata({
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        })
        assert um["input_cache_read_tokens"] == 0
        assert um["input_cache_creation_tokens"] == 0
        assert um["output_reasoning_tokens"] == 0

    def test_none_input(self):
        um = normalize_usage_metadata(None)
        assert um["input_tokens"] == 0
        assert um["output_tokens"] == 0
        assert um["total_tokens"] == 0
        assert um["input_cache_read_tokens"] == 0
        assert um["input_cache_creation_tokens"] == 0
        assert um["output_reasoning_tokens"] == 0

    def test_no_prompt_details_fallback(self):
        """When prompt_tokens_details is missing for OpenAI, flat Anthropic fields are used."""
        usage = {
            "input_tokens": 120,
            "output_tokens": 40,
            "total_tokens": 160,
            "cache_read_input_tokens": 90,
            "cache_creation_input_tokens": 30,
            "reasoning_tokens": 20,
        }
        um = normalize_usage_metadata(usage)
        assert um["input_cache_read_tokens"] == 90
        assert um["input_cache_creation_tokens"] == 30
        assert um["output_reasoning_tokens"] == 20
