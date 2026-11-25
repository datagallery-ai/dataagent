"""Read, validate and merge configuration layers into effective Settings.

The product file is Home `config.yaml`, or the explicit `--config` file when
that flag is set. Plugin `plugins.config` is merged underneath the chosen file.
Dictionaries merge by field; independent Hooks accumulate in layer order. Other
lists and scalars replace earlier values. Defaults fill only missing fields.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import ValidationError, create_model

from dataagent.bootstrap.paths import absolute, output_directory, read_home_template
from dataagent.config.declarations import StrictModel, normalize_hooks
from dataagent.config.settings import (
    SECTION_MODELS,
    ModelSettings,
    SemanticSettings,
    Settings,
)
from dataagent.config.strict_yaml import check_yaml_path, parse_yaml, read_yaml
from dataagent.diagnostics.errors import DataAgentError

ENV_REFERENCE = re.compile(r"\$env\{([A-Za-z_][A-Za-z0-9_]*)\}")
Scope = Literal["user", "cli", "plugin"]


class MissingEnvironmentError(ValueError):
    """A `$env{NAME}` reference has no value. Hosts start unready instead of exiting."""


class ConfigurationLoadError(DataAgentError, ValueError):
    """The selected product file was not loaded."""

    error_code = "CONFIG_ERROR"

    def __init__(self, message: str):
        super().__init__(source="config", component="configuration", fact=message)


@dataclass(frozen=True)
class ConfigSource:
    """A configuration file's participation record, without its content or credentials."""

    scope: Scope
    path: Path
    applied: bool


@dataclass(frozen=True)
class Layer:
    scope: Scope
    path: Path
    data: Mapping = field(repr=False)
    env: Mapping = field(repr=False)
    home: Path
    base_dir: Path | None = None
    dotenv: Path | None = None


def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@lru_cache
def _partial(model):
    """Use the same field schemas with omitted fields allowed, never default-filled."""
    fields = {}
    for name, info in model.model_fields.items():
        annotation = info.annotation
        if annotation in SECTION_MODELS:
            annotation = _partial(annotation)
        elif model is Settings and name == "semantic":
            annotation = _partial(SemanticSettings) | None
        elif model is Settings and name == "models":
            annotation = dict[str, _partial(ModelSettings)]
        fields[name] = (
            Annotated[annotation, *info.metadata] if info.metadata else annotation,
            None,
        )
    return create_model(f"{model.__name__}Layer", __base__=StrictModel, **fields)


def validate_against(model, value, source):
    try:
        return model.model_validate(value)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'configuration'}: {item['msg']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        raise ValueError(f"{source}: {details}") from None


def expand_env(value, env, *, source, home, location=(), dotenv=None):
    hint = home / ".env" if dotenv is None else dotenv
    if isinstance(value, str):

        def replace(match):
            name = match.group(1)
            if not env.get(name):
                raise MissingEnvironmentError(
                    f"{source}: missing environment variable {name} for "
                    f"{'.'.join(map(str, location))}; configure {hint}"
                )
            return env[name]

        return ENV_REFERENCE.sub(replace, value)
    if isinstance(value, Mapping):
        return {
            key: expand_env(
                item, env, source=source, home=home, location=(*location, key), dotenv=dotenv
            )
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            expand_env(
                item, env, source=source, home=home, location=(*location, index), dotenv=dotenv
            )
            for index, item in enumerate(value)
        ]
    return value


def _expand_model_value(value, field_sources, location):
    """Expand the selected model's environment references with each leaf's own layer.

    ``overlay`` records a source per scalar leaf and per whole array, so
    mapping fields (e.g. ``extra_body``) resolve each leaf with the layer
    that wrote that leaf, while an array and everything nested inside it
    (elements, objects, deeper arrays) expands with the layer that wrote
    the array.
    """
    if isinstance(value, Mapping):
        return {
            key: _expand_model_value(item, field_sources, (*location, key))
            for key, item in value.items()
        }
    source = field_sources[location]
    return expand_env(
        value,
        source.env,
        source=source.path,
        home=source.home,
        location=location,
        dotenv=source.dotenv,
    )


def load_layers(
    home: Path,
    env: Mapping,
    cli_config: Path | None,
    *,
    dotenv: Path | None = None,
    preview_home: bool = False,
) -> tuple[Layer, ...]:
    """Load the selected product file: explicit `--config`, otherwise Home."""
    layers = []
    for scope, path in _config_sources(home, cli_config):
        try:
            layer = _load_layer(
                scope, path, env=env, home=home, dotenv=dotenv, preview_home=preview_home
            )
        except MissingEnvironmentError:
            raise  # Same handling as a missing model variable: fixable in .env, not a file error.
        except ValueError as error:
            raise ConfigurationLoadError(str(error)) from None
        if layer is not None:
            layers.append(layer)
    return tuple(layers)


def merge(layers: tuple[Layer, ...]) -> Settings:
    """Apply layers in the given low-to-high order; this function does not sort them.

    Missing keys inherit earlier values. Dictionaries merge recursively (including
    models by ID and field). dataagent.hooks accumulates; [] adds nothing. Other lists
    and scalars are replaced, so [] still clears e.g. plugins.enabled or plugins.paths.
    Resolve the selected model's environment references and fill defaults only after
    every layer has been applied. Input layers remain unchanged.
    """
    merged, field_sources = {}, {}

    def overlay(target, overrides, source, prefix=()):
        for key, value in overrides.items():
            location = (*prefix, key)
            if isinstance(value, Mapping):
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                overlay(target.get(key), value, source, location)
            elif location == ("dataagent", "hooks") or (
                len(location) == 5
                and location[:2] == ("plugins", "config")
                and location[3] == "run_hooks"
                and location[4] in {"before_run", "after_run"}
            ):
                # Each registration keeps its position; repeated events/functions are valid.
                if not isinstance(value, (tuple, list)):
                    raise ValueError(f"{source.path}: {'.'.join(location)} must be a list")
                target.setdefault(key, []).extend(value)
            else:
                # No other list changes semantics: the later layer wins.
                target[key] = value
                field_sources[location] = source

    # Callers supply plugin defaults first, then the one selected product file.
    for layer in layers:
        overlay(merged, layer.data, layer)

    # Only now is the effective model known; each field keeps its winning layer's source.
    selected = merged.get("dataagent", {}).get("model", "primary")
    if not isinstance(selected, str):
        selected = ""  # Settings validation reports the wrong type.
    model_values = merged.get("models", {}).get(selected, {})
    for key, value in model_values.items():
        model_values[key] = _expand_model_value(value, field_sources, ("models", selected, key))

    # Defaults fill only still-absent fields; no layer's defaults can mask earlier values.
    sources = ", ".join(str(layer.path) for layer in layers) or "Effective configuration"
    return validate_against(Settings, merged, sources)


def _config_sources(home: Path, cli_config: Path | None) -> list[tuple[Scope, Path]]:
    """Use `--config` by itself. Otherwise use Home `config.yaml`."""
    if cli_config is not None:
        return [("cli", cli_config)]
    return [("user", home / "config.yaml")]


def _load_layer(
    scope: Scope,
    path: Path,
    *,
    env: Mapping,
    home: Path,
    dotenv: Path | None = None,
    preview_home: bool = False,
) -> Layer | None:
    """Read one file; only an absent automatic configuration may be skipped."""
    check_yaml_path(path)
    if not path.exists() and not path.is_symlink():
        if scope == "cli":
            raise ValueError(f"Configuration file does not exist: {path}")
        if scope != "user" or not preview_home:
            return None
        raw = parse_yaml(read_home_template("config.yaml"), path)
    elif not path.is_file():
        raise ValueError(f"Expected a configuration file: {path}")
    else:
        raw = read_yaml(path)
    if scope == "plugin" and (
        set(raw) - {"plugins"}
        or not isinstance(raw.get("plugins", {}), dict)
        or set(raw.get("plugins", {})) - {"config"}
    ):
        raise ValueError(f"{path}: plugin defaults may only contribute plugins.config")

    data = {}
    for key, value in raw.items():
        if key == "models":
            # Resolve only the selected model's references, after layers are merged.
            data[key] = value
            continue
        if key == "dataagent" and isinstance(value, Mapping) and "hooks" in value:
            try:
                value = {**value, "hooks": normalize_hooks(value["hooks"])}
            except ValueError as error:
                raise ValueError(f"{path}: dataagent.hooks: {error}") from None
        data[key] = expand_env(value, env, source=path, home=home, location=(key,), dotenv=dotenv)

    # Validate without filling defaults: omitted fields must not override lower layers.
    validate_against(_partial(Settings), data, path)
    plugins = data.get("plugins", {})
    semantic = data.get("semantic")
    if isinstance(semantic, dict):
        tls = semantic.get("tls", {})
        for name in ("ca_file", "client_cert", "client_key"):
            value = tls.get(name)
            if value is not None:
                tls[name] = absolute(value, path.parent, expand_home=True).resolve()
    # Partial models retain field schemas, not PluginSettings' model validators.
    if "enabled" in plugins and len(set(plugins["enabled"])) != len(plugins["enabled"]):
        raise ValueError(f"{path}: plugins.enabled contains duplicates")
    if "paths" in plugins:
        plugins["paths"] = tuple(
            absolute(item, path.parent, expand_home=True).resolve() for item in plugins["paths"]
        )
    agent_data = data.get("dataagent")
    if isinstance(agent_data, dict) and "workspaces" in agent_data:
        agent_data["workspaces"] = _resolve_workspace_entries(agent_data["workspaces"], path)
    if isinstance(agent_data, dict) and agent_data.get("output_root") is not None:
        try:
            agent_data["output_root"] = output_directory(agent_data["output_root"])
        except ValueError as error:
            raise ValueError(f"{path}: dataagent.output_root: {error}") from error
    return Layer(scope=scope, path=path, data=freeze(data), env=env, home=home, dotenv=dotenv)


def _resolve_workspace_entries(entries, source: Path):
    if not isinstance(entries, (list, tuple)):
        raise ValueError(f"{source}: dataagent.workspaces must be a list")
    resolved = []
    names, paths = [], []
    for index, item in enumerate(entries):
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise ValueError(f"{source}: dataagent.workspaces[{index}].path must be a string")
        path = absolute(item["path"], source.parent, expand_home=True).resolve()
        resolved.append({**item, "path": path})
        names.append(item.get("name"))
        paths.append(path)
    if len(set(names)) != len(names):
        raise ValueError(f"{source}: dataagent.workspaces contains duplicate names")
    if len(set(paths)) != len(paths):
        raise ValueError(f"{source}: dataagent.workspaces contains duplicate paths")
    return resolved
