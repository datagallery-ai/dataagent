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
"""Tests for multi-segment prompt append building."""

from dataagent.config.config_manager import build_prompt_append


def test_build_prompt_append_single_content() -> None:
    tpl = build_prompt_append({"content": "hello"})
    assert "hello" in tpl.content


def test_build_prompt_append_list_concatenation() -> None:
    tpl = build_prompt_append(
        [
            {"content": "part-a"},
            {"content": "part-b"},
        ]
    )
    assert "part-a" in tpl.content
    assert "part-b" in tpl.content
    assert tpl.content.index("part-a") < tpl.content.index("part-b")
