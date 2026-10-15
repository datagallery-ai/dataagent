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
"""可注册 HITL 分类器框架（hitl_classifier）单元测试：关键词匹配 + 修正 override 链。

注册顺序即优先级、override 收到原始 question——这些是场景侧（de_agent suite）
保持回放门 182 问分布不变的行为等价前提。
"""

from __future__ import annotations

import dataclasses

import pytest

from dataagent.core.flex.hooks.hitl_classifier import HitlActionSpec, HitlQuestionClassifier

SPEC_A = HitlActionSpec(name="ACTION_A", keywords=("alpha", "阿尔法"))
SPEC_B = HitlActionSpec(name="ACTION_B", keywords=("beta",))


def _make(*specs: HitlActionSpec, default: str = "DEFAULT", overrides=()) -> HitlQuestionClassifier:
    return HitlQuestionClassifier(specs or (SPEC_A, SPEC_B), default_action=default, overrides=overrides)


class TestHitlActionSpec:
    def test_frozen_dataclass(self):
        spec = HitlActionSpec(name="X", keywords=("kw",))
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "Y"

    def test_fields(self):
        spec = HitlActionSpec(name="X", keywords=("kw1", "kw2"))
        assert spec.name == "X"
        assert spec.keywords == ("kw1", "kw2")


class TestKeywordMatch:
    def test_first_registered_spec_wins(self):
        """两个 spec 的关键词同时命中时，注册序在前的生效（顺序即优先级）。"""
        assert _make().classify("alpha beta") == "ACTION_A"

    def test_second_spec_when_first_misses(self):
        assert _make().classify("only beta") == "ACTION_B"

    def test_no_match_returns_default(self):
        assert _make().classify("gamma") == "DEFAULT"

    def test_empty_question_returns_default(self):
        assert _make().classify("") == "DEFAULT"

    def test_none_question_returns_default(self):
        assert _make().classify(None) == "DEFAULT"

    def test_question_matched_case_insensitively(self):
        assert _make().classify("ALPHA 大写") == "ACTION_A"

    def test_keyword_case_insensitive(self):
        spec = HitlActionSpec(name="MIXED", keywords=("TaBle",))
        clf = HitlQuestionClassifier((spec,), default_action="DEFAULT")
        assert clf.classify("确认 table 选择") == "MIXED"

    def test_substring_containment_match(self):
        """关键词是小写包含匹配（非全词），与既有 HITL_ACTION_KEYWORDS 语义一致。"""
        assert _make().classify("请确认阿尔法口径") == "ACTION_A"


class TestOverrideChain:
    def test_override_result_wins(self):
        clf = _make(overrides=[lambda q, a: "REROUTED"])
        assert clf.classify("alpha") == "REROUTED"

    def test_none_override_passes_to_next(self):
        clf = _make(overrides=[lambda q, a: None, lambda q, a: "SECOND"])
        assert clf.classify("alpha") == "SECOND"

    def test_first_non_none_override_wins(self):
        clf = _make(overrides=[lambda q, a: "FIRST", lambda q, a: "SECOND"])
        assert clf.classify("alpha") == "FIRST"

    def test_all_none_overrides_keep_base(self):
        clf = _make(overrides=[lambda q, a: None])
        assert clf.classify("alpha") == "ACTION_A"

    def test_override_receives_original_question_and_base_action(self):
        """override 收到未小写的原始 question 与关键词阶段结果。"""
        seen: list[tuple[str, str]] = []

        def spy(question: str, base_action: str):
            seen.append((question, base_action))
            return None

        assert _make(overrides=[spy]).classify("Alpha") == "ACTION_A"
        assert seen == [("Alpha", "ACTION_A")]

    def test_override_runs_on_default_base(self):
        """无关键词命中（base=default）时 override 链仍执行。"""
        clf = _make(overrides=[lambda q, a: "Z"])
        assert clf.classify("unrelated") == "Z"

    def test_empty_question_skips_overrides(self):
        """空问题在 override 链之前短路，override 不被调用。"""
        called: list[str] = []

        def override(question: str, base_action: str):
            called.append(question)
            return "X"

        assert _make(overrides=[override]).classify("") == "DEFAULT"
        assert called == []
