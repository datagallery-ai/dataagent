"""YAML configuration for product, plugin and SubAgent declarations."""

import json
from pathlib import Path

import pytest

from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.config.declarations import PluginSpec
from dataagent.config.settings import PluginSettings
from dataagent.config.strict_yaml import read_yaml
from dataagent.extensions.loading import select_plugins
from dataagent.extensions.subagents import collect_subagents


@pytest.mark.parametrize("kind", ["product", "plugin", "subagent"])
@pytest.mark.parametrize(
    "text, error",
    [
        ("key: 1\nkey: 2", "duplicate mapping key"),
        ("key: [SECRET_VALUE", "invalid YAML syntax"),
    ],
)
def test_every_config_entrypoint_uses_strict_yaml(tmp_path, kind, text, error):
    root = tmp_path / "sample"
    root.mkdir()
    path = (
        root / {"product": "config.yaml", "plugin": ".plugin.yaml", "subagent": "child.yaml"}[kind]
    )
    path.write_text(text)
    with pytest.raises(ValueError, match=error):
        if kind == "product":
            prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))
        elif kind == "plugin":
            select_plugins(PluginSettings(enabled=("sample",)), (root,))
        else:
            collect_subagents(root, PluginSpec(id="sample", subagents=["child.yaml"]), [], set())


def test_all_shipped_json_and_document_examples_are_valid():
    import re

    product = Path(__file__).resolve().parents[1]
    for path in [
        product / "config.example.yaml",
        product / "examples/config.yaml",
        product / "examples/nl2sql-entry.yaml",
        product / "dataagent/bootstrap/templates/config.yaml",
        *list((product / "dataagent/plugins").rglob("*.yaml")),
        *list((product / "examples/plugins").rglob("*.yaml")),
    ]:
        if path.name != "templates.yaml":  # Business IR templates have their own loader.
            assert isinstance(read_yaml(path), dict)
    for path in [
        product / "README.md",
        *list((product / "docs").glob("*.md")),
        product / "examples/README.md",
    ]:
        for block in re.findall(r"```json\n(.*?)\n```", path.read_text(), flags=re.S):
            json.loads(block)


def test_external_plugin_and_subagent_reference_yaml(common_plugin):
    selected = select_plugins(PluginSettings(enabled=("common",)), (common_plugin,))
    root, plugin = selected[0]
    assert plugin.subagents == ["subagents/general-purpose.yaml"]
    children = []
    collect_subagents(root, plugin, children, set())
    assert children[0][2].name == "general-purpose"
    # Skills keep the format expected by Deep Agents, independent of config serialization.
    assert (root / "skills/number-summary/SKILL.md").read_text().startswith("---\n")
    assert (root / "skills/tabular-inspection/SKILL.md").read_text().startswith("---\n")
    assert read_yaml(root / ".mcp.yaml") == {"mcpServers": {}}
