"""Read, validate and merge configuration layers into effective Settings.

File precedence: Home < explicit CLI config. Dictionaries merge by
field; independent Hooks accumulate in layer order. Other lists and scalars replace
earlier values. Defaults fill only missing fields.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import ValidationError, create_model

from dataagent.bootstrap.paths import absolute
from dataagent.declarations import StrictModel, normalize_hooks
from dataagent.settings import (
    SECTION_MODELS,
    ModelSettings,
    Settings,
)
from dataagent.strict_json import read_json

ENV_REFERENCE = re.compile(r"\$env\{([A-Za-z_][A-Za-z0-9_]*)\}")
Scope = Literal["user", "cli"]


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
        elif model is Settings and name == "models":
            annotation = dict[str, _partial(ModelSettings)]
        fields[name] = (Annotated[annotation, *info.metadata] if info.metadata else annotation, None)
    return create_model(f"{model.__name__}Layer", __base__=StrictModel, **fields)


def validate_against(model, value, source):
    try:
        return model.model_validate(value)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'configuration'}: {item['msg']}"
            for item in error.errors(include_input=False, include_context=False, include_url=False)
        )
        if isinstance(value, Mapping) and "workspace" in value:
            details += "; use --workspace instead of a workspace configuration field"
        raise ValueError(f"{source}: {details}") from None


def expand_env(value, env, *, source, home, location=()):
    if isinstance(value, str):
        def replace(match):
            name = match.group(1)
            if not env.get(name):
                raise ValueError(
                    f"{source}: missing environment variable {name} for "
                    f"{'.'.join(map(str, location))}; configure {home / '.env'}"
                )
            return env[name]
        return ENV_REFERENCE.sub(replace, value)
    if isinstance(value, Mapping):
        return {key: expand_env(item, env, source=source, home=home, location=(*location, key))
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [expand_env(item, env, source=source, home=home, location=(*location, index))
                for index, item in enumerate(value)]
    return value


def load_layers(home: Path, env: Mapping, cli_config: Path | None) -> tuple[Layer, ...]:
    """Load present files in low-to-high order: Home, explicit config."""
    layers = []
    for scope, path in _config_sources(home, cli_config):
        layer = _load_layer(scope, path, env=env, home=home)
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
                overlay(target.setdefault(key, {}), value, source, location)
            elif location == ("dataagent", "hooks"):
                # Each registration keeps its position; repeated events/functions are valid.
                target.setdefault(key, []).extend(value)
            else:
                # No other list changes semantics: the later layer wins.
                target[key] = value
                field_sources[location] = source

    # load_layers supplies Home -> CLI. Later layers have higher priority.
    for layer in layers:
        overlay(merged, layer.data, layer)

    # Only now is the effective model known; each field keeps its winning layer's source.
    selected = merged.get("dataagent", {}).get("model", {}).get("default", "primary")
    model_values = merged.get("models", {}).get(selected, {})
    for key, value in model_values.items():
        location = ("models", selected, key)
        source = field_sources[location]
        model_values[key] = expand_env(
            value, source.env, source=source.path, home=source.home, location=location,
        )

    # Defaults fill only still-absent fields; no layer's defaults can mask earlier values.
    sources = ", ".join(str(layer.path) for layer in layers) or "Effective configuration"
    return validate_against(Settings, merged, sources)


def _config_sources(home: Path, cli_config: Path | None) -> list[tuple[Scope, Path]]:
    """Order Home < CLI; an explicit file replaces its automatic occurrence."""
    candidates: list[tuple[Scope, Path]] = [("user", home / "config.json")]
    if cli_config is not None:
        cli_real = cli_config.resolve()
        candidates = [(scope, path) for scope, path in candidates
                      if path.resolve() != cli_real]
        candidates.append(("cli", cli_config))
    return candidates


def _load_layer(scope: Scope, path: Path, *, env: Mapping, home: Path) -> Layer | None:
    """Read one file; only an absent automatic configuration may be skipped."""
    if not path.exists() and not path.is_symlink():
        if scope == "cli":
            raise ValueError(f"Configuration file does not exist: {path}")
        return None
    if not path.is_file():
        raise ValueError(f"Expected a configuration file: {path}")

    raw = read_json(path)
    # File-level migration checks; individual Hook declarations use the shared schema below.
    if raw.get("schema_version", 3) != 3:
        raise ValueError(
            f"{path}: configuration schema_version must be 3; migrate hooks to "
            "event + Python entrypoint and remove hook_handlers/handler/events/match"
        )
    agent = raw.get("dataagent", {})
    if isinstance(agent, Mapping) and (removed := {"middleware", "callbacks"} & agent.keys()):
        raise ValueError(
            f"{path}: dataagent.{', dataagent.'.join(sorted(removed))} is unsupported; "
            "remove these fields (including empty lists) and use hooks with "
            "event + Python entrypoint + params"
        )
    if isinstance(agent, Mapping) and "hook_handlers" in agent:
        raise ValueError(
            f"{path}: legacy hook configuration is unsupported; use hooks with "
            "event + Python entrypoint + params"
        )

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
        data[key] = expand_env(value, env, source=path, home=home, location=(key,))

    # Validate without filling defaults: omitted fields must not override lower layers.
    validate_against(_partial(Settings), data, path)
    plugins = data.get("plugins", {})
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
    return Layer(scope=scope, path=path, data=freeze(data), env=env, home=home)


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
