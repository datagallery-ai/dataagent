import json
import os

import pytest

from dataagent.config_migration import apply_migration, plan_migration
from dataagent.strict_yaml import read_yaml


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))
    return path


def closure(tmp_path):
    home = tmp_path / "home"
    plugin = tmp_path / "demo"
    write(home / "config.json", {"plugins": {"paths": ["../demo"]}, "secret": "$env{SECRET}"})
    write(home / ".mcp.json", {"mcpServers": {}})
    write(
        plugin / ".plugin.json",
        {"id": "demo", "defaults_file": "defaults.json", "subagents": ["child.json"]},
    )
    write(plugin / "defaults.json", {"plugins": {"config": {"demo": {"dataset": "data.json"}}}})
    write(plugin / "child.json", {"name": "child", "prompt": "中文\n多行\n"})
    write(plugin / ".mcp.json", {"mcpServers": {}})
    write(plugin / "data.json", {"business": True})
    return home, plugin


def test_preview_apply_closure_and_idempotence(tmp_path):
    home, plugin = closure(tmp_path)
    originals = {p: p.read_bytes() for p in tmp_path.rglob("*.json")}
    plan, notices = plan_migration(homes=[home])
    assert len(plan) == 6 and not notices
    assert not list(tmp_path.rglob("*.yaml"))
    apply_migration(plan)
    assert read_yaml(plugin / ".plugin.yaml")["subagents"] == ["child.yaml"]
    assert (
        read_yaml(plugin / "defaults.yaml")["plugins"]["config"]["demo"]["dataset"] == "data.json"
    )
    assert not (plugin / "data.yaml").exists()
    assert originals == {p: p.read_bytes() for p in originals}
    for item in plan:
        assert item.target.stat().st_mode & 0o777 == 0o600
    stamps = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*.yaml")}
    again, _ = plan_migration(homes=[home])
    assert all(item.action == "already_migrated" for item in again)
    apply_migration(again)
    assert stamps == {p: p.stat().st_mtime_ns for p in stamps}


def test_conflict_preflight_and_exclusive_race(tmp_path):
    home, plugin = closure(tmp_path)
    (plugin / "child.yaml").write_text("conflict: true\n")
    with pytest.raises(ValueError, match="conflict"):
        plan_migration(homes=[home])
    assert not (home / "config.yaml").exists()
    (plugin / "child.yaml").unlink()
    plan, _ = plan_migration(homes=[home])
    plan[-1].target.write_text("concurrent: true")
    with pytest.raises(FileExistsError):
        apply_migration(plan)
    assert list(tmp_path.rglob("*.yaml")) == [plan[-1].target]


def test_failed_write_rolls_back(tmp_path, monkeypatch):
    home, _ = closure(tmp_path)
    plan, _ = plan_migration(homes=[home])
    original = os.fsync
    count = 0

    def fail(fd):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected failure")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        apply_migration(plan)
    assert not list(tmp_path.rglob("*.yaml"))


@pytest.mark.parametrize("data", [{"n": 2**53}, {"n": -(2**53)}])
def test_large_integer_rejected_before_write(tmp_path, data):
    path = write(tmp_path / "single.json", data)
    with pytest.raises(ValueError):
        plan_migration(files=[path])
    assert not path.with_suffix(".yaml").exists()


def test_plugin_boundary_and_env_notice(tmp_path):
    home, plugin = closure(tmp_path)
    write(plugin / ".plugin.json", {"defaults_file": "../home/config.json"})
    with pytest.raises(ValueError, match="escapes"):
        plan_migration(plugins=[plugin])
    path = write(tmp_path / "standalone.json", {"plugins": {"paths": ["$env{PLUGIN}"]}})
    plan, notices = plan_migration(files=[path])
    assert len(plan) == 1 and len(notices) == 1
    assert "$env{PLUGIN}" in plan[0].text


def test_legacy_home_does_not_initialize_over_source(tmp_path):
    from dataagent.bootstrap.paths import initialize_home

    write(tmp_path / "config.json", {"server": {"port": 8888}})
    with pytest.raises(ValueError, match="migrate"):
        initialize_home(tmp_path)
    assert not (tmp_path / "config.yaml").exists()


def test_explicit_yml_and_old_suffix(tmp_path):
    path = tmp_path / "team.yml"
    path.write_text("server:\n  port: 8811\n")
    assert read_yaml(path)["server"]["port"] == 8811
    path = write(tmp_path / "team.json", {})
    with pytest.raises(ValueError, match="migrate"):
        read_yaml(path)


def test_equivalence_does_not_confuse_boolean_and_number(tmp_path):
    source = write(tmp_path / "single.json", {"value": 1})
    source.with_suffix(".yaml").write_text("value: true\n")
    with pytest.raises(ValueError, match="conflict"):
        plan_migration(files=[source])


def test_cli_file_selection_does_not_include_home(tmp_path, monkeypatch):
    from dataagent.config_migration import run
    from serve.__main__ import _parser

    home, _ = closure(tmp_path)
    monkeypatch.setenv("DATAAGENT_HOME", str(home))
    source = write(tmp_path / "single.json", {"message": "中文"})
    assert run(_parser().parse_args(["config", "migrate", "--file", str(source)])) == 0
    assert not list(tmp_path.rglob("*.yaml"))
    assert run(_parser().parse_args(["config", "migrate", "--file", str(source), "--apply"])) == 0
    assert source.with_suffix(".yaml").exists()
    assert not (home / "config.yaml").exists()


@pytest.mark.parametrize('token', ['008', '0o12', '1e2', 'true', 'null', '.inf', '中文', 'line\nnext\n'])
def test_yaml_core_string_values_and_keys_migrate_without_type_changes(tmp_path, token):
    value = {'models': {'primary': {'name': token}}, token: token}
    source = write(tmp_path / 'strings.json', value)
    original = source.read_bytes()
    plan, _ = plan_migration(files=[source])
    assert not source.with_suffix('.yaml').exists()
    apply_migration(plan)
    assert read_yaml(source.with_suffix('.yaml')) == value
    assert source.read_bytes() == original
    before = source.with_suffix('.yaml').stat().st_mtime_ns
    repeated, _ = plan_migration(files=[source])
    assert repeated[0].action == 'already_migrated'
    apply_migration(repeated)
    assert source.with_suffix('.yaml').stat().st_mtime_ns == before
