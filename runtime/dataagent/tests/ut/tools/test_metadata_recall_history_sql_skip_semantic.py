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
"""History-SQL metadata_recall should not run semantic_retrieve_context_loader."""

from __future__ import annotations

from dataagent.actions.tools.semantic_tool.metadata_recall import (
    DEFAULT_METADATA_RECALL_TEMPERATURE,
    _SEMANTIC_RETRIEVE_HOOK,
    _is_history_sql_query,
    _resolve_metadata_recall_temperature,
    _strip_semantic_retrieve_hook,
)


def test_is_history_sql_query_detects_marker() -> None:
    assert _is_history_sql_query("检索历史SQL作为参考")
    assert _is_history_sql_query("检索历史sql作为参考")
    assert not _is_history_sql_query("检索表和列 schema")


def test_strip_semantic_retrieve_hook_removes_only_that_entry() -> None:
    config = {
        "HOOKS": {
            "agent": {
                "pre": ["other_hook", _SEMANTIC_RETRIEVE_HOOK, "another_hook"],
            }
        }
    }
    _strip_semantic_retrieve_hook(config)
    assert config["HOOKS"]["agent"]["pre"] == ["other_hook", "another_hook"]


def test_strip_semantic_retrieve_hook_cleans_empty_hook_tree() -> None:
    config = {"HOOKS": {"agent": {"pre": [_SEMANTIC_RETRIEVE_HOOK]}}}
    _strip_semantic_retrieve_hook(config)
    assert "HOOKS" not in config


def test_resolve_metadata_recall_temperature_defaults_to_0_4() -> None:
    assert DEFAULT_METADATA_RECALL_TEMPERATURE == 0.4
    assert _resolve_metadata_recall_temperature(None) == 0.4
    assert _resolve_metadata_recall_temperature({}) == 0.4
    assert _resolve_metadata_recall_temperature({"temperature": 0.2}) == 0.2
    assert _resolve_metadata_recall_temperature({"temperature": "0.6"}) == 0.6
    assert _resolve_metadata_recall_temperature({"temperature": "bad"}) == 0.4
