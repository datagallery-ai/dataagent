"""One strict JSON format for product, plugin and SubAgent declarations."""

import json
from pathlib import Path

import pytest

from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.declarations import PluginSpec
from dataagent.extensions.loading import select_plugins
from dataagent.extensions.subagents import collect_subagents
from dataagent.settings import PluginSettings
from dataagent.strict_json import read_json


def test_json_preserves_unicode_environment_references_and_types(tmp_path):
    path = tmp_path / "config.json"
    value = {"中文": "$env{MODEL}", "enabled": True, "values": [None, 1, 0.5, {}]}
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    assert read_json(path) == value


@pytest.mark.parametrize("text, error", [
    ('{"secret-key": 1, "secret-key": 2}', "Duplicate JSON key"),
    ('{"outer": {"key": 1, "key": 2}}', "Duplicate JSON key"),
    ('{"key": "SECRET_VALUE",}', "Invalid JSON"),
    ('{"key": NaN}', "Non-finite"),
    ('{"key": Infinity}', "Non-finite"),
    ('{"key": -Infinity}', "Non-finite"),
    ('{"key": 1e9999}', "finite range"),
    ('{"key": -1e9999}', "finite range"),
    ('{"key": 1} // comment', "Invalid JSON"),
    ('{"key": 1} {}', "Invalid JSON"),
    ("key: SECRET_VALUE", "Invalid JSON"),
    ("", "Invalid JSON"),
    *[(value, "Expected a JSON object") for value in ["[]", "null", '"SECRET_VALUE"', "1"]],
])
def test_invalid_json_is_rejected_without_source_disclosure(tmp_path, text, error):
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=error) as caught:
        read_json(path)
    message = str(caught.value)
    assert str(path) in message
    assert "SECRET_VALUE" not in message and "secret-key" not in message


def test_invalid_utf8_does_not_disclose_source(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b'{"key": "SECRET_VALUE\xff"}')
    with pytest.raises(ValueError, match="Invalid JSON") as caught:
        read_json(path)
    assert "SECRET_VALUE" not in str(caught.value)


@pytest.mark.parametrize("kind", ["product", "plugin", "subagent"])
@pytest.mark.parametrize("text, error", [
    ('{"key": 1, "key": 2}', "Duplicate JSON key"),
    ("key: yaml-is-not-json", "Invalid JSON"),
])
def test_every_config_entrypoint_uses_strict_json(tmp_path, kind, text, error):
    root = tmp_path / "sample"
    root.mkdir()
    path = root / {"product": "config.json", "plugin": ".plugin.json",
                   "subagent": "child.json"}[kind]
    path.write_text(text)
    with pytest.raises(ValueError, match=error):
        if kind == "product":
            prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))
        elif kind == "plugin":
            select_plugins(PluginSettings(enabled=("sample",)), (root,))
        else:
            collect_subagents(root, PluginSpec(id="sample", subagents=["child.json"]), [], set())


def test_all_shipped_json_and_document_examples_are_valid():
    import re

    product = Path(__file__).resolve().parents[1]
    for path in [
        product / "config.example.json", product / "examples/config.json",
        product / "dataagent/bootstrap/templates/config.json",
        *list((product / "builtin-plugins").rglob("*.json")),
        *list((product / "examples/plugins").rglob("*.json")),
    ]:
        assert isinstance(read_json(path), dict)
    for path in [product / "README.md", *list((product / "docs").glob("*.md")),
                 product / "examples/README.md"]:
        for block in re.findall(r"```json\n(.*?)\n```", path.read_text(), flags=re.S):
            json.loads(block)


def test_external_plugin_and_subagent_reference_json(common_plugin):
    selected = select_plugins(PluginSettings(enabled=("common",)), (common_plugin,))
    root, plugin = selected[0]
    assert plugin.subagents == ["subagents/general-purpose.json"]
    children = []
    collect_subagents(root, plugin, children, set())
    assert children[0][2].name == "general-purpose"
    # Skills keep the format expected by Deep Agents, independent of config serialization.
    assert (root / "skills/number-summary/SKILL.md").read_text().startswith("---\n")
    assert (root / "skills/tabular-inspection/SKILL.md").read_text().startswith("---\n")
    assert read_json(root / ".mcp.json") == {"mcpServers": {}}
