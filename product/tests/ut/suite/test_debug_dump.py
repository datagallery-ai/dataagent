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


def test_dump_merged_config_uses_custom_runtime_dump_dir(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = {
        "AGENT_CONFIG": {"name": "x"},
        "WORKSPACE_POLICY": {"layout": {"runtime_dump_dir": "debug/runtime"}},
    }
    target = dump_merged_config(settings, workspace=workspace)
    assert target is not None
    assert target.parent == workspace / "debug" / "runtime"


def test_format_settings_yaml_redacts_long_api_key_keeping_length_and_edges() -> None:
    """Long secrets keep first/last 4 chars; middle is stars; total length is unchanged."""
    raw = "sk-SECRETKEY1234567890"
    text = format_settings_yaml({"MODEL": {"api_key": raw}})
    redacted = "sk-S**************7890"
    assert len(raw) == 22
    assert len(redacted) == 22
    assert redacted in text
    assert raw not in text


def test_format_settings_yaml_redacts_long_password_keeping_length() -> None:
    raw = "supersecretpassword"
    text = format_settings_yaml({"SEMANTIC_LAYER": {"password": raw}})
    redacted = "supe***********word"
    assert len(raw) == len(redacted) == 19
    assert redacted in text
    assert raw not in text


def test_format_settings_yaml_masks_short_secrets_entirely() -> None:
    """Values of length <= 8 become all stars and keep the original length."""
    text = format_settings_yaml(
        {
            "AUTH": {
                "token": "abcd1234",
                "passwd": "short",
            }
        }
    )
    assert "********" in text
    assert "*****" in text
    assert "abcd1234" not in text
    assert "short" not in text


def test_format_settings_yaml_redacts_nested_and_suffixed_keys() -> None:
    raw_key = "sk-SECRETKEY1234567890"
    raw_secret = "clientsecretvalue99"
    settings = {
        "MODEL": {
            "providers": [
                {"openai_api_key": raw_key, "name": "chat"},
            ]
        },
        "HOOKS": {"client_secret": raw_secret, "Authorization": "BearerTokenValueXX"},
    }
    text = format_settings_yaml(settings)
    assert "sk-S**************7890" in text
    assert "clie***********ue99" in text
    assert "Bear**********ueXX" in text
    assert raw_key not in text
    assert raw_secret not in text
    assert "BearerTokenValueXX" not in text
    assert "name: chat" in text


def test_format_settings_yaml_leaves_non_string_secrets_unchanged() -> None:
    """Non-string values are left as-is; only strings are redacted."""
    text = format_settings_yaml({"AUTH": {"password": 12345678, "token": True, "api_key": None}})
    assert "12345678" in text
    assert "true" in text.lower()
    assert "null" in text or "none" in text.lower()


def test_dump_merged_config_writes_redacted_secrets_without_mutating_input(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    raw = "sk-SECRETKEY1234567890"
    settings = {"MODEL": {"api_key": raw}, "HOOKS": {"nodes": {}}}
    target = dump_merged_config(settings, workspace=workspace)
    assert target is not None
    content = target.read_text(encoding="utf-8")
    assert "sk-S**************7890" in content
    assert raw not in content
    assert settings["MODEL"]["api_key"] == raw
