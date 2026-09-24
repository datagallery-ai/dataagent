import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dataagent.bootstrap import LaunchOptions, prepare_runtime
from dataagent.bootstrap.config_files import Layer, freeze, load_layers, merge
from dataagent.bootstrap.paths import RuntimePaths, WorkspaceInput
from dataagent.strict_json import read_json


def test_config_environment_precedence_and_path_anchor(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "schema_version": 3,
        "models": {"primary": {
            "name": "$env{LLM_MODEL}", "base_url": "https://example.invalid/v1",
            "api_key": "$env{LLM_API_KEY}",
        }},
        "plugins": {"paths": ["./extra"]},
    }))
    config.with_name(".env").write_text("LLM_MODEL=file-model\nLLM_API_KEY=not-for-output\n")
    monkeypatch.setenv("LLM_MODEL", "environment-model")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    settings = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config)).settings
    assert settings.models["primary"].name == "environment-model"
    assert settings.plugins.paths == (tmp_path / "extra",)
    assert "not-for-output" not in repr(settings)
    assert "LLM_API_KEY" not in __import__("os").environ


def test_default_does_not_read_cwd_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text("not valid JSON")
    with pytest.raises(ValueError, match="models: Field required"):
        prepare_runtime(LaunchOptions())


def test_duplicate_json_and_unknown_settings(tmp_path, settings):
    path = tmp_path / "duplicate.json"
    path.write_text('{"x": 1, "x": 2}')
    with pytest.raises(ValueError, match="Duplicate"):
        read_json(path)
    with pytest.raises(ValidationError, match="Extra inputs"):
        type(settings).model_validate({**settings.model_dump(), "shell": True})


def test_import_comes_from_agentkit():
    import dataagent
    assert Path(dataagent.__file__).parent.parent.name == "agentkit"


@pytest.mark.parametrize("legacy, expected", [
    ({"schema_version": 2}, "migrate hooks"),
    ({"dataagent": {"hook_handlers": {"audit": {"entrypoint": "hooks/audit.py:handle"}}}},
     "use hooks with event + python entrypoint + params"),
    ({"dataagent": {"hooks": [{"handler": "audit", "events": ["agent.before"]}]}},
     "python functions only: use event + entrypoint + params"),
    *(({"dataagent": {"hooks": [{"event": "before_agent", "entrypoint": "hooks/audit.py:handle", **field}]}},
       "python functions only: use event + entrypoint + params")
      for field in [{"handler": "audit"}, {"events": ["agent.before"]}, {"match": {"tools": ["tool"]}}]),
])
def test_legacy_hook_configuration_has_migration_error(tmp_path, legacy, expected):
    path = tmp_path / "old-config.json"
    path.write_text(json.dumps(legacy))
    with pytest.raises(ValueError) as error:
        prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))
    message = str(error.value).lower()
    assert str(path).lower() in message and expected in message


@pytest.mark.parametrize("hook", [
    {"event": "before_model", "entrypoint": "policy.py:handle", "timeout_seconds": 2},
    {"event": "before_model", "command": ["python", "policy.py"]},
    *({"event": event, "entrypoint": "policy.py:handle"}
      for event in ["model_error", "tool_error", "agent_error"]),
])
def test_removed_hook_protocol_is_rejected_without_version_guessing(tmp_path, hook):
    path = tmp_path / "removed-hooks.json"
    path.write_text(json.dumps({"schema_version": 3, "dataagent": {"hooks": [hook]}}))
    with pytest.raises(ValueError) as error:
        prepare_runtime(LaunchOptions(cwd=tmp_path, config=path))
    assert str(path) in str(error.value)


@pytest.fixture
def layer_paths(tmp_path):
    return RuntimePaths.resolve(tmp_path / "home", (WorkspaceInput("workspace-0", tmp_path),))


def test_load_layers_skips_missing_automatic_files_but_requires_cli(layer_paths):
    assert load_layers(layer_paths.home, {}, None) == ()
    missing = layer_paths.home.parent / "missing.json"
    with pytest.raises(ValueError, match="Configuration file does not exist") as error:
        load_layers(layer_paths.home, {}, missing)
    assert str(missing) in str(error.value)


@pytest.mark.parametrize("scope", ["user", "cli"])
@pytest.mark.parametrize("shape", ["directory", "dangling-symlink"])
def test_load_layers_does_not_skip_invalid_file_shapes(layer_paths, scope, shape):
    path = {"user": layer_paths.home / "config.json",
            "cli": layer_paths.home.parent / "explicit.json"}[scope]
    path.parent.mkdir(parents=True, exist_ok=True)
    if shape == "directory":
        path.mkdir()
    else:
        path.symlink_to(layer_paths.home.parent / "missing-target.json")
    with pytest.raises(ValueError, match="Expected a configuration file") as error:
        load_layers(layer_paths.home, {}, path if scope == "cli" else None)
    assert str(path) in str(error.value)


def test_load_layers_cli_symlink_deduplicates_real_file_but_retains_path_anchor(layer_paths):
    source = layer_paths.home / "config.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"plugins": {"paths": ["./local-plugin"]}}))
    alias = layer_paths.home.parent / "override/config.json"
    alias.parent.mkdir()
    alias.symlink_to(source)
    layers = load_layers(layer_paths.home, {}, alias)
    assert len(layers) == 1
    assert layers[0].scope == "cli" and layers[0].path == alias
    assert layers[0].data["plugins"]["paths"] == (alias.parent / "local-plugin",)


def test_load_layers_preserves_partial_fields_and_deferred_models_in_frozen_data(layer_paths):
    layer_paths.home.mkdir()
    path = layer_paths.home / "config.json"
    path.write_text(json.dumps({
        "models": {"primary": {"name": "$env{MISSING_MODEL}"}},
        "dataagent": {"limits": {"model_calls_per_agent": "$env{CALLS}"}},
        "plugins": {"enabled": []},
    }))
    environment = {"CALLS": "4"}
    layer, = load_layers(layer_paths.home, environment, None)
    assert layer.path == path and layer.home == layer_paths.home
    assert layer.env == environment
    assert set(layer.data) == {"models", "dataagent", "plugins"}
    assert layer.data["models"]["primary"] == {"name": "$env{MISSING_MODEL}"}
    assert layer.data["dataagent"] == {"limits": {"model_calls_per_agent": "4"}}
    assert layer.data["plugins"] == {"enabled": ()}
    with pytest.raises(TypeError):
        layer.data["server"] = {}
    with pytest.raises(TypeError):
        layer.data["models"]["primary"]["name"] = "changed"


def merge_layer(tmp_path, scope, data, env=None):
    return Layer(scope, tmp_path / f"{scope}.json", freeze(data), env or {}, tmp_path / f"{scope}-home")


@pytest.fixture
def merge_data():
    return {"models": {"primary": {
        "name": "base-model", "base_url": "https://example.invalid/v1", "api_key": "synthetic-key",
    }}}


def test_merge_uses_input_order_and_recursive_fields_without_mutating_layers(tmp_path, merge_data):
    # Deliberately reverse the usual scopes: merge must not reorder its input.
    first = merge_layer(tmp_path, "cli", {
        **merge_data, "server": {"port": 8800},
        "dataagent": {"limits": {"timeout_seconds": 30, "model_calls_per_agent": 3}},
    })
    last = merge_layer(tmp_path, "user", {
        "models": {"primary": {"name": "last-model"}}, "server": {"port": 8801},
        "dataagent": {"limits": {"model_calls_per_agent": 7}},
    })
    settings = merge((first, last))
    assert settings.models["primary"].name == "last-model"
    assert settings.models["primary"].base_url == "https://example.invalid/v1"
    assert settings.dataagent.limits.model_calls_per_agent == 7
    assert settings.dataagent.limits.timeout_seconds == 30
    assert settings.server.port == 8801
    assert settings.server.host == "127.0.0.1" and settings.dataagent.limits.tool_calls_per_agent == 128
    assert first.data["models"] == merge_data["models"]
    assert first.data["dataagent"]["limits"] == {"timeout_seconds": 30, "model_calls_per_agent": 3}
    assert last.data["models"]["primary"] == {"name": "last-model"}
    assert last.data["server"] == {"port": 8801}


@pytest.mark.parametrize("override, expected", [
    (None, ("first", "backup")), ([], ()), (["second"], ("second",)),
])
def test_merge_lists_replace_clear_or_inherit_when_omitted(tmp_path, merge_data, override, expected):
    first = merge_layer(tmp_path, "user", {
        **merge_data, "plugins": {"enabled": ["first", "backup"], "paths": [tmp_path / "plugin"]},
    })
    last = merge_layer(tmp_path, "cli", {} if override is None else {"plugins": {"enabled": override}})
    settings = merge((first, last))
    assert settings.plugins.enabled == expected
    assert settings.plugins.paths == (tmp_path / "plugin",)
    assert first.data["plugins"]["enabled"] == ("first", "backup")


def test_merge_expands_only_final_model_using_each_fields_source_environment(tmp_path):
    first = merge_layer(tmp_path, "user", {"models": {
        "primary": {"name": "$env{UNUSED}", "base_url": "$env{UNUSED}", "api_key": "$env{UNUSED}"},
        "alternate": {"name": "$env{SHADOWED}", "base_url": "https://example.invalid/$env{VALUE}",
                      "api_key": "$env{VALUE}"},
    }}, {"VALUE": "first"})
    last = merge_layer(tmp_path, "cli", {
        "dataagent": {"model": {"default": "alternate"}},
        "models": {"alternate": {"name": "$env{VALUE}", "api_key": "$env{VALUE}"}},
    }, {"VALUE": "last"})
    settings = merge((first, last))
    selected = settings.models["alternate"]
    assert selected.name == "last" and selected.api_key.get_secret_value() == "last"
    assert selected.base_url == "https://example.invalid/first"
    assert settings.models["primary"].name == "$env{UNUSED}"
    assert first.data["models"]["alternate"]["name"] == "$env{SHADOWED}"
    assert last.data["models"]["alternate"]["name"] == "$env{VALUE}"


def test_merge_missing_model_variable_reports_field_source_file_and_home(tmp_path, merge_data):
    merge_data["models"]["primary"]["name"] = "$env{NEEDED}"
    first = merge_layer(tmp_path, "user", merge_data)
    last = merge_layer(tmp_path, "cli", {"server": {"port": 8801}}, {"NEEDED": "wrong-layer-value"})
    with pytest.raises(ValueError) as error:
        merge((first, last))
    message = str(error.value)
    assert str(first.path) in message and str(first.home / ".env") in message
    assert "models.primary.name" in message and "NEEDED" in message
    assert str(last.path) not in message and "wrong-layer-value" not in message
