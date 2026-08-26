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
"""Tests for LLM n-gram repetition detection."""

from dataagent.core.managers.llm_manager.llm_client import (
    LLMErrorCategory,
    LLMRepetitionError,
    _detect_ngram_repetition,
    _detect_repetition,
    _repetition_thresholds,
)


def test_repetition_error_carries_category_and_str():
    """LLMRepetitionError 必须携带 category=REPETITION_DETECTED 供重试链路与 __str__ 使用。

    回归 guard: 加 docstring 时曾误删 ``self.category = LLMErrorCategory.REPETITION_DETECTED``，
    导致 ``e.category`` 访问抛 AttributeError、``str(err)`` 失败。
    """
    err = LLMRepetitionError("ngram", "repeat detected", content_snippet="abc" * 100, model="qwen-test")
    assert err.category == LLMErrorCategory.REPETITION_DETECTED
    assert err.detection_type == "ngram"
    assert err.detail == "repeat detected"
    assert err.content_snippet == ("abc" * 100)[:500]
    assert err.model == "qwen-test"
    assert err.app_recoverable is True
    # __str__ 访问 self.category, 不应 AttributeError
    text = str(err)
    assert "repetition_detected" in text
    assert "ngram" in text
    assert "repeat detected" in text


def test_equals_only_ngrams_are_ignored():
    """Repeated equals separators should not trigger n-gram detection."""
    is_repetition, detail = _detect_ngram_repetition("=" * 200, window=8, max_repeat=1)

    assert not is_repetition
    assert detail is None


def test_repeated_content_around_equals_is_still_detected():
    """Non-equals repetition should remain detectable around equals separators."""
    content = "= repeated content =\n" * 10

    is_repetition, detail = _detect_ngram_repetition(content, window=2, max_repeat=2)

    assert is_repetition
    assert detail is not None


def test_repetition_leniency_scales_thresholds():
    """leniency 越大：次数类阈值升高，多样性阈值降低。"""
    tight = _repetition_thresholds(1.0)
    loose = _repetition_thresholds(3.0)

    assert loose["ngram_max_repeat"] == tight["ngram_max_repeat"] * 3
    assert loose["char_cycle_min"] == tight["char_cycle_min"] * 3
    assert loose["tool_call_max_repeat"] == tight["tool_call_max_repeat"] * 3
    assert abs(loose["char_diversity_min"] - tight["char_diversity_min"] / 3) < 1e-9


def test_detect_repetition_respects_leniency():
    """同一结构化 JSON 片段在默认系数下可能误报，提高 leniency 后应放过。"""
    item = '{"feature":"x","value":1},'
    # 构造足够长、且 n-gram 会反复命中分隔符的文本
    content = "[" + item * 80 + "]"
    is_rep_tight, _ = _detect_repetition(content, leniency=1.0)
    is_rep_loose, _ = _detect_repetition(content, leniency=5.0)
    assert is_rep_tight
    assert not is_rep_loose
