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

from dataagent.core.managers.llm_manager.llm_client import _detect_ngram_repetition


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
