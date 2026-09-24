import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

from dataagent.bootstrap import (
    LaunchOptions,
    RuntimePaths,
    initialize_home,
    prepare_runtime,
)
from dataagent.bootstrap.paths import WorkspaceInput, absolute
from dataagent.bootstrap.paths import directory as resolve_directory

MODEL = {"models": {"primary": {
    "name": "test-model", "base_url": "https://example.invalid/v1", "api_key": "private-test-value",
}}}


def config(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def options(tmp_path, **kwargs):
    cli = config(tmp_path / "explicit.json", MODEL)
    return LaunchOptions(cwd=tmp_path, workspace=tmp_path, config=cli, **kwargs)


def test_prepare_has_no_sdk_initialization_or_workspace_writes(tmp_path):
    runtime = prepare_runtime(options(tmp_path))
    assert not runtime.paths.home.exists()
    assert runtime.paths.state_dir == tmp_path / "home/runtime"
    assert runtime.model_name == "test-model"
    assert runtime.timeout_seconds == 180
    assert "private-test-value" not in repr(runtime)
    with pytest.raises(FrozenInstanceError):
        runtime.paths = None
    with pytest.raises(TypeError):
        runtime.settings.models["new"] = None
    with pytest.raises(FrozenInstanceError):
        runtime.extensions.hooks = ()


def test_cwd_never_selects_configuration_or_extensions(tmp_path):
    home = tmp_path / "home"
    config(home / "config.json", MODEL)
    runtimes = []
    for name in ("first", "second"):
        cwd = tmp_path / name
        source = cwd / ".dataagent"
        source.mkdir(parents=True)
        (source / "config.json").write_text("malformed: must not be read")
        for kind in ("skills", "hooks", "plugins"):
            (source / kind).write_text("not a directory: must not be inspected")
        runtimes.append(prepare_runtime(LaunchOptions(cwd=cwd)))
    assert runtimes[0] == runtimes[1]
    assert runtimes[0].paths.workspaces == ()
    assert not (home / "runtime").exists()
    assert not (home / "workspaces").exists()


def test_switching_home_switches_configuration_extensions_and_state(tmp_path, monkeypatch):
    runtimes = []
    for name in ("first", "second"):
        home = tmp_path / name
        config(home / "config.json", {"models": {"primary": {
            "name": name, "base_url": "https://example.invalid/v1", "api_key": "test",
        }}})
        skill = home / "skills/example/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: example\ndescription: test\n---\nExample skill")
        monkeypatch.setenv("DATAAGENT_HOME", str(home))
        runtime = prepare_runtime(LaunchOptions(cwd=tmp_path))
        assert runtime.model_name == name
        assert runtime.paths.home == home
        assert runtime.paths.state_dir == home / "runtime"
        assert (str(home / "skills"), "User") in runtime.extensions.skill_sources
        assert runtime.paths.for_session("example").outputs.is_relative_to(home)
        runtimes.append(runtime)
    assert runtimes[0].paths.workspaces == runtimes[1].paths.workspaces == ()


def test_explicit_config_can_clear_inputs_without_synthesizing_default(tmp_path):
    config(tmp_path / "home/config.json", {**MODEL, "dataagent": {
        "workspaces": [{"name": "data", "path": str(tmp_path)}],
    }})
    cli = config(tmp_path / "empty.json", {"dataagent": {"workspaces": []}})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    assert runtime.paths.workspaces == runtime.settings.dataagent.workspaces == ()
    assert not (runtime.paths.home / "workspaces").exists()


def test_runtime_separates_settings_paths_extensions_and_report(tmp_path):
    runtime = prepare_runtime(options(tmp_path))
    assert {field.name for field in fields(runtime)} == {
        "settings", "paths", "extensions", "report",
    }
    assert {field.name for field in fields(runtime.report)} == {
        "configs", "plugin_origins", "env_file",
    }
    assert {field.name for field in fields(runtime.extensions)} == {
        "plugin_roots", "skill_sources", "hooks", "mcp_servers",
    }
    assert runtime.extensions.hooks == ()
    assert not hasattr(runtime, "sources")
    assert not hasattr(runtime, "resources")
    assert not hasattr(runtime, "provenance")
    assert not hasattr(runtime.paths, "project_dir")
    with pytest.raises(FrozenInstanceError):
        runtime.report.plugin_origins = ()


def test_home_initialization_is_exclusive_and_non_overwriting(tmp_path):
    home = tmp_path / "home"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(initialize_home, [home] * 8))
    assert {item.name for item in home.iterdir()} == {
        "config.json", ".env", "skills", "hooks", "plugins", "runtime",
    }
    assert home.stat().st_mode & 0o777 == 0o700
    assert (home / ".env").stat().st_mode & 0o777 == 0o600
    (home / ".env").write_text("USER_CONTENT=keep\n")
    initialize_home(home)
    assert (home / ".env").read_text() == "USER_CONTENT=keep\n"
    (home / "config.json").unlink()
    initialize_home(home)
    assert json.loads((home / "config.json").read_text())["schema_version"] == 3


def test_home_resolution_and_initialization_stay_separate(tmp_path):
    from dataagent.bootstrap import paths

    assert initialize_home is paths.initialize_home
    home = paths.home_path(tmp_path)
    assert home == tmp_path / "home"
    runtime_paths = RuntimePaths.resolve(home, (WorkspaceInput("workspace-0", tmp_path),))
    assert runtime_paths.home == home
    assert not home.exists()

    initialize_home(home)
    assert (home / "config.json").is_file()


def test_template_error_has_variable_field_and_help_path_without_values(tmp_path):
    with pytest.raises(ValueError) as error:
        prepare_runtime(LaunchOptions(cwd=tmp_path, init_home=True))
    message = str(error.value)
    assert "LLM_MODEL" in message and "models.primary.name" in message
    assert str(tmp_path / "home/.env") in message
    assert not (tmp_path / ".dataagent").exists()


def test_two_layers_append_hooks_but_replace_plugin_lists_and_scalars(tmp_path):
    cli = options(tmp_path).config
    config(tmp_path / "home/config.json", {
        **MODEL, "models": {**MODEL["models"], "unused": {
            "name": "$env{NOT_SET}", "base_url": "https://example.invalid", "api_key": "$env{NO_KEY}",
        }},
        "dataagent": {"limits": {"timeout_seconds": 90},
                      "hooks": [{"entrypoint": "hooks/audit.py:handle", "event": "after_agent"}]},
        "plugins": {"enabled": ["common"], "paths": ["./user-extra"]},
    })
    config(cli, {"models": {"primary": {"name": "explicit-model"}},
                 "plugins": {"enabled": []}, "server": {"port": 8801}, "dataagent": {
        "hooks": [{"entrypoint": "hooks/cli.py:handle", "event": "after_agent"}],
    }})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, workspace=tmp_path, config=cli))
    assert runtime.model_name == "explicit-model"
    assert runtime.settings.server.port == 8801
    assert runtime.timeout_seconds == 90
    assert runtime.settings.plugins.enabled == ()
    assert runtime.settings.plugins.paths == (tmp_path / "home/user-extra",)
    assert [hook.entrypoint for hook in runtime.settings.dataagent.hooks] == [
        "hooks/audit.py:handle", "hooks/cli.py:handle",
    ]
    assert [binding.base_dir for binding in runtime.extensions.hooks] == [
        tmp_path / "home", tmp_path,
    ]
    assert all(binding.spec is spec for binding, spec in
               zip(runtime.extensions.hooks, runtime.settings.dataagent.hooks, strict=True))
    with pytest.raises(FrozenInstanceError):
        runtime.extensions.hooks[0].base_dir = tmp_path / "other"
    assert [item.scope for item in runtime.report.configs if item.applied] == ["user", "cli"]


@pytest.mark.parametrize("scope", ["user", "cli"])
def test_workspace_field_is_rejected_in_every_layer(tmp_path, scope):
    opts = options(tmp_path)
    path = {"user": tmp_path / "home/config.json",
            "cli": opts.config}[scope]
    config(path, {**MODEL, "workspace": {"path": str(tmp_path)}})
    with pytest.raises(ValueError) as error:
        prepare_runtime(opts)
    assert str(path) in str(error.value) and "--workspace" in str(error.value)


@pytest.mark.parametrize("scope", ["user", "cli"])
def test_invalid_overridden_fields_still_fail_at_their_source(tmp_path, scope):
    opts = options(tmp_path)
    path = {"user": tmp_path / "home/config.json",
            "cli": opts.config}[scope]
    config(path, {"dataagent": {"limits": {"model_calls_per_agent": "secret-invalid-value"}}})
    with pytest.raises(ValueError) as error:
        prepare_runtime(opts)
    assert str(path) in str(error.value)
    assert "secret-invalid-value" not in str(error.value)


@pytest.mark.parametrize("scope", ["user", "cli"])
def test_duplicate_plugins_fail_at_source_even_when_cli_overrides_them(tmp_path, scope):
    opts = options(tmp_path)
    config(opts.config, {**MODEL, "plugins": {"enabled": []}})
    path = tmp_path / ("home/config.json" if scope == "user" else "explicit.json")
    config(path, {"plugins": {"enabled": ["common", "common"]}})
    with pytest.raises(ValueError) as error:
        prepare_runtime(opts)
    assert str(error.value) == f"{path}: plugins.enabled contains duplicates"


def test_environment_precedence_empty_values_dedup_and_process_unchanged(tmp_path, monkeypatch, capsys):
    opts = options(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("LLM_MODEL=home\nLLM_API_KEY=home-key\nDATAAGENT_V2_TOKEN=bad\n")
    (tmp_path / ".env").write_text("LLM_MODEL=sibling\nLLM_API_KEY=\n")
    env_file = tmp_path / "explicit.env"
    env_file.write_text("LLM_MODEL=explicit\nDATAAGENT_HOME=/ignored\n")
    config(opts.config, {"models": {"primary": {
        "name": "$env{LLM_MODEL}", "base_url": "https://example.invalid", "api_key": "$env{LLM_API_KEY}",
    }}})
    before = dict(os.environ)
    runtime = prepare_runtime(replace(opts, env_file=env_file))
    assert runtime.model_name == "explicit"
    assert runtime.redaction_secrets == ("home-key",)
    assert dict(os.environ) == before
    stderr = capsys.readouterr().err
    assert "DATAAGENT_HOME" in stderr and "DATAAGENT_V2_TOKEN" in stderr
    assert "/ignored" not in stderr and "bad" not in stderr
    monkeypatch.setenv("LLM_MODEL", "process")
    assert prepare_runtime(replace(opts, env_file=env_file)).model_name == "process"
    monkeypatch.setenv("LLM_MODEL", "")
    assert prepare_runtime(replace(opts, env_file=env_file)).model_name == "explicit"


def test_workspace_precedence_and_missing_directory(tmp_path, monkeypatch):
    opts = options(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("DATAAGENT_WORKSPACE", "first")
    assert prepare_runtime(replace(opts, workspace=None)).paths.workspaces[0].path == first
    assert prepare_runtime(replace(opts, workspace="second")).paths.workspaces[0].path == second
    with pytest.raises(ValueError, match="does not exist"):
        prepare_runtime(replace(opts, workspace="missing"))
    assert not (tmp_path / "missing").exists()
    with pytest.raises(ValueError, match="directory"):
        prepare_runtime(replace(opts, workspace=opts.config))


def test_path_helpers_distinguish_lexical_and_real_paths_without_creating_directories(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    lexical = absolute("alias/missing", tmp_path)
    assert lexical == alias / "missing"
    assert resolve_directory(lexical) == real / "missing"
    assert not (real / "missing").exists()
    with pytest.raises(ValueError, match="does not exist"):
        resolve_directory(lexical, required=True)
    assert not (real / "missing").exists()


@pytest.mark.parametrize("source", ["home", "config", "explicit"])
def test_relative_workspace_from_env_uses_launch_cwd_not_env_directory(tmp_path, source):
    cwd = tmp_path / "launch"
    workspace = cwd / "work"
    workspace.mkdir(parents=True)
    cli = config(tmp_path / "configuration/config.json", MODEL)
    env_file = {"home": tmp_path / "home/.env", "config": cli.with_name(".env"),
                "explicit": tmp_path / "environment/settings.env"}[source]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("DATAAGENT_WORKSPACE=work\n")
    runtime = prepare_runtime(LaunchOptions(cwd=cwd, config=cli,
                                    env_file=env_file if source == "explicit" else None))
    assert runtime.paths.workspaces[0].path == workspace
    assert not (env_file.parent / "work").exists()


def test_hidden_empty_project_and_legacy_paths_are_ignored(tmp_path):
    opts = options(tmp_path)
    config(tmp_path / "config.json", {"invalid": True})
    (tmp_path / ".dataagent_v2").mkdir()
    for name in ("skills", "hooks", "plugins", "state"):
        root = tmp_path / ".dataagent" / name
        root.mkdir(parents=True)
        (root / ".gitkeep").touch()
    runtime = prepare_runtime(opts)
    assert runtime.extensions.skill_sources == ()
    assert runtime.paths.state_dir == tmp_path / "home/runtime"


def test_explicit_symlinks_keep_declaration_anchor(tmp_path):
    target = config(tmp_path / "storage/config.json", {**MODEL, "dataagent": {
        "hooks": [{"entrypoint": "hooks/check.py:handle", "event": "before_model"}],
    }})
    env_target = tmp_path / "storage/settings.env"
    env_target.write_text("UNUSED_TEST_SETTING=value\n")
    declared = tmp_path / "declared"
    declared.mkdir()
    cli, env_file = declared / "config.json", declared / "settings.env"
    cli.symlink_to(target)
    env_file.symlink_to(env_target)

    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli, env_file=env_file))
    assert runtime.extensions.hooks[0].base_dir == declared
    assert runtime.settings.dataagent.hooks[0].entrypoint == "hooks/check.py:handle"
    assert runtime.report.env_file == env_file
    assert [(item.scope, item.path) for item in runtime.report.configs if item.applied] == [("cli", cli)]


def test_default_prepare_uses_call_time_cwd_and_does_not_init_home(tmp_path, monkeypatch):
    config(tmp_path / "home/config.json", MODEL)
    monkeypatch.chdir(tmp_path)
    assert prepare_runtime().paths.workspaces == ()
    assert prepare_runtime(LaunchOptions()).settings.models["primary"].name == "test-model"
    assert not (tmp_path / "home/skills").exists()


def test_env_syntax_error_is_redacted(tmp_path):
    opts = options(tmp_path)
    env_file = tmp_path / "bad.env"
    env_file.write_text('BAD="secret-on-broken-line\n')
    with pytest.raises(ValueError) as error:
        prepare_runtime(replace(opts, env_file=env_file))
    assert "line 1" in str(error.value) and str(env_file) in str(error.value)
    assert "secret-on-broken-line" not in str(error.value)


def test_home_fallback_and_missing_os_home(tmp_path, monkeypatch):
    opts = options(tmp_path)
    monkeypatch.setenv("DATAAGENT_HOME", "  ")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-user"))
    assert prepare_runtime(opts).paths.home == tmp_path / "fake-user/.dataagent"
    def missing_home(cls):
        raise RuntimeError("unavailable")
    monkeypatch.setattr(Path, "home", classmethod(missing_home))
    with pytest.raises(ValueError, match="set DATAAGENT_HOME"):
        prepare_runtime(opts)


def test_nested_home_permissions_and_existing_items_preserved(tmp_path):
    home = tmp_path / "new-parent/home"
    initialize_home(home)
    for directory in (home.parent, home, home / "skills", home / "hooks", home / "plugins"):
        assert directory.stat().st_mode & 0o777 == 0o700
    (home / "config.json").chmod(0o640)
    initialize_home(home)
    assert (home / "config.json").stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("target", ["home", "home/runtime"])
def test_directory_slots_cannot_be_ordinary_files(tmp_path, target):
    opts = options(tmp_path)
    path = tmp_path / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("preserve this file")
    with pytest.raises(ValueError, match="directory"):
        prepare_runtime(opts)
    assert path.read_text() == "preserve this file"


def test_default_home_symlink_and_state_symlink_resolve(tmp_path, monkeypatch):
    user = tmp_path / "fake-user"
    user.mkdir()
    real_home = tmp_path / "real-home"
    config(real_home / "config.json", MODEL)
    (user / ".dataagent").symlink_to(real_home, target_is_directory=True)
    monkeypatch.delenv("DATAAGENT_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    state = tmp_path / "state-real"
    state.mkdir()
    (real_home / "runtime").symlink_to(state, target_is_directory=True)
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path))
    assert runtime.paths.home == real_home and runtime.paths.state_dir == state


def test_same_environment_file_only_applied_once_at_highest_position(tmp_path):
    from dataagent.bootstrap.environment import load_env

    first, second = tmp_path / "first.env", tmp_path / "second.env"
    first.write_text("DEDUP_VALUE=first\n")
    second.write_text("DEDUP_VALUE=second\n")
    alias = tmp_path / "alias.env"
    alias.symlink_to(first)
    result = load_env((first, second), alias)
    assert result.values["DEDUP_VALUE"] == "first"
    assert result.files == (second, alias)
    with pytest.raises(ValueError, match="does not exist"):
        load_env((), tmp_path / "missing.env")


def test_empty_list_replaces_but_absent_list_preserves(tmp_path):
    config(tmp_path / "home/config.json", {**MODEL, "plugins": {"enabled": [], "paths": ["./one"]}})
    cli = config(tmp_path / "override.json", {"plugins": {"paths": []}})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    assert runtime.settings.plugins.enabled == () and runtime.settings.plugins.paths == ()


def test_empty_hook_list_adds_nothing_and_preserves_lower_layer_without_importing(tmp_path):
    entry = {"entrypoint": "missing.py:handle", "event": "before_agent"}
    config(tmp_path / "home/config.json", {**MODEL, "dataagent": {"hooks": [entry]}})
    cli = config(tmp_path / "override.json", {"dataagent": {"hooks": []}})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    hook, = runtime.settings.dataagent.hooks
    assert hook.entrypoint == "missing.py:handle"
    binding, = runtime.extensions.hooks
    assert binding.base_dir == tmp_path / "home" and binding.spec is hook


def test_hook_merge_is_repeatable_without_mutating_layers_or_hook_config(tmp_path):
    from dataagent.bootstrap.config_files import Layer, freeze, merge

    layers = tuple(Layer(scope, tmp_path / scope / "config.json", freeze({
        **(MODEL if scope == "user" else {}), "dataagent": {"hooks": [{
            "event": "before_agent", "entrypoint": "audit.py:handle",
            "params": {"dataagent": {"hooks": [scope]}},
        }]},
    }), {}, tmp_path / "home") for scope in ("user", "cli"))
    first, second = merge(layers), merge(layers)
    assert first == second
    assert [hook.params for hook in first.dataagent.hooks] == [
        {"dataagent": {"hooks": [scope]}} for scope in ("user", "cli")
    ]
    assert all(len(layer.data["dataagent"]["hooks"]) == 1 for layer in layers)


@pytest.mark.parametrize("scope", ["user", "cli"])
@pytest.mark.parametrize("field", ["middleware", "callbacks"])
@pytest.mark.parametrize("value", [[], [{"entrypoint": "must-not-import.py:create"}]])
def test_removed_factory_fields_fail_at_every_source_even_when_overridden(tmp_path, scope, field, value):
    opts = options(tmp_path)
    source = {"user": tmp_path / "home/config.json",
              "cli": opts.config}[scope]
    config(source, {**MODEL, "dataagent": {field: value}})
    with pytest.raises(ValueError) as caught:
        prepare_runtime(opts)
    assert str(source) in str(caught.value)
    assert f"dataagent.{field}" in str(caught.value)
    assert "use hooks" in str(caught.value)


@pytest.mark.parametrize("scope", ["user", "cli"])
def test_hook_lists_accumulate_with_each_declarations_own_directory(tmp_path, scope):
    roots = {"user": tmp_path / "home", "cli": tmp_path / "explicit"}
    config(roots["user"] / "config.json", MODEL)
    applied = []
    for layer in ("user", "cli"):
        applied.append(layer)
        config(roots[layer] / "hooks.json" if layer == "cli" else roots[layer] / "config.json", {
            **(MODEL if layer == "user" else {}), "dataagent": {"hooks": [
                {"event": event, "entrypoint": f"{layer}.py:handle"}
                for event in ("before_agent", "after_agent")
            ]},
        })
        if layer == scope:
            break
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, workspace=tmp_path,
                                   config=roots["cli"] / "hooks.json" if scope == "cli" else None))
    assert [binding.base_dir for binding in runtime.extensions.hooks] == [
        roots[layer] for layer in applied for _ in range(2)
    ]
    assert [hook.entrypoint for hook in runtime.settings.dataagent.hooks] == [
        f"{layer}.py:handle" for layer in applied for _ in range(2)
    ]
    assert [hook.event for hook in runtime.settings.dataagent.hooks] == ["before_agent", "after_agent"] * len(applied)
    assert all(binding.spec is spec for binding, spec in
               zip(runtime.extensions.hooks, runtime.settings.dataagent.hooks, strict=True))
    assert not hasattr(runtime.extensions, "hooks_base_dir")


def test_higher_layer_without_hooks_preserves_lower_hook_directory(tmp_path):
    config(tmp_path / "home/config.json", {**MODEL, "dataagent": {"hooks": [{
        "event": "before_agent", "entrypoint": "hooks/audit.py:handle",
    }]}})
    cli = config(tmp_path / "explicit/config.json", {"dataagent": {
        "limits": {"timeout_seconds": 60},
    }})
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=cli))
    assert runtime.extensions.hooks[0].base_dir == tmp_path / "home"
    assert runtime.settings.dataagent.hooks[0].entrypoint == "hooks/audit.py:handle"
    assert runtime.timeout_seconds == 60


@pytest.mark.parametrize("explicit_workspace", [False, True])
def test_rest_cli_and_sdk_prepare_same_launch_options(tmp_path, monkeypatch, explicit_workspace):
    from restapi import __main__ as entry

    opts = options(tmp_path, init_home=True)
    if not explicit_workspace:
        opts = replace(opts, workspace=None)
    expected = prepare_runtime(opts)
    captured = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entry, "serve", lambda runtime, **kwargs: captured.append(runtime) or 0)
    args = ["serve", "--config", str(opts.config)]
    if explicit_workspace:
        args.extend(["--workspace", str(opts.workspace)])
    assert entry.main(args) == 0
    assert captured[0].settings == expected.settings
    assert captured[0].paths == expected.paths


def test_home_creation_failure_preserves_already_created_items(tmp_path, monkeypatch):
    home = tmp_path / "new-home"
    original_open = os.open
    def fail_env(path, *args, **kwargs):
        if Path(path).name == ".env":
            raise PermissionError("test failure")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", fail_env)
    with pytest.raises(PermissionError):
        initialize_home(home)
    assert (home / "config.json").exists() and (home / "skills").is_dir()
    assert not (home / ".env").exists()
