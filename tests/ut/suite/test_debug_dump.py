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
"""Tests for runtime configuration debug dump."""

from dataagent.core.suite.debug_dump import dump_merged_config, format_settings_yaml


def test_format_settings_yaml_inserts_blank_lines_between_top_level_keys() -> None:
    text = format_settings_yaml(
        {
            "ACTOR_LOOP": [{"node": "planner"}],
            "POST_WORKFLOW": [],
            "HOOKS": {"nodes": {}},
        }
    )
    assert "ACTOR_LOOP:" in text
    assert "POST_WORKFLOW:" in text
    assert "HOOKS:" in text
    assert text.index("HOOKS:") < text.index("ACTOR_LOOP:")
    assert "\n\nACTOR_LOOP:" in text
    assert "\n\nPOST_WORKFLOW:" in text


def test_dump_merged_config_writes_dataagent_config_file(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = {"AGENT_CONFIG": {"name": "x"}, "HOOKS": {"nodes": {}}}
    target = dump_merged_config(settings, workspace=workspace)
    assert target is not None
    assert target.parent == workspace / ".runtime"
    assert target.name.startswith("dataagent_config_")
    assert target.name.endswith(".yaml")
    content = target.read_text(encoding="utf-8")
    assert "AGENT_CONFIG:" in content
    assert "\n\nHOOKS:" in content
