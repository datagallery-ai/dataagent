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
import copy
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from dataagent.utils.constants import DEFAULT_WORKSPACE_LAYOUT
from dataagent.utils.env_file_loader import load_env_file
from dataagent.utils.log import logger

env_path = Path(os.getcwd()) / ".env"
if env_path.exists():
    load_env_file(env_path)
    logger.trace(f"Loaded environment variables from: {env_path}")


class ConfigManager:
    """Configuration manager"""

    def __init__(self, config_path: Path | None = None):
        """
        Initialize configuration manager

        Args:
            config_path (str): Configuration files path
        """
        self.config_path = Path(config_path) if config_path else None
        self.settings: dict[str, Any] = {}
        self.activated_suites: list[dict[str, str]] = []
        self._lock = threading.Lock()
        self.last_reload = None

        # Initial configuration loading
        # 注意：这里只加载用户指定的单个配置文件，不自动加载默认配置。
        # 推荐使用 DataAgent.from_config() 入口，它会自动处理默认配置 + 用户配置的合并。
        # 如果直接使用 ConfigManager(config_path)，只会加载该配置文件。
        if self.config_path:
            self.reload(str(self.config_path))

    @staticmethod
    def merge_configs(base_config, override_config):
        """
        Merge two configuration mappings using ``merge_layers``.

        Unlike legacy ``_deep_merge``, list-valued keys (``TOOLS.*``, ``HOOKS`` slots,
        ``SUBAGENTS``, workflow lists, etc.) are **appended** with the override
        layer before the base layer. Dict/scalar keys in the override still win.

        When ``override_config`` contains ``OVERRIDE_KEYS``, each listed top-level key
        that is also written in ``override_config`` replaces the merged value entirely.

        Args:
            base_config: Lower-priority mapping (treated as the default layer).
            override_config: Higher-priority mapping (treated as the user layer).

        Returns:
            Merged configuration dict.
        """
        from dataagent.core.suite.merge import apply_override_keys, merge_layers, parse_override_keys
        from dataagent.utils.constants import META_OVERRIDE_KEYS

        base = ConfigManager._normalize_subagent_alias(base_config or {})
        override = ConfigManager._normalize_subagent_alias(override_config or {})
        override_keys = parse_override_keys(override)
        user_layer = copy.deepcopy(override)
        user_layer.pop(META_OVERRIDE_KEYS, None)
        result = merge_layers([base, user_layer])
        apply_override_keys(result, user_layer, override_keys)
        result.pop(META_OVERRIDE_KEYS, None)
        return result

    @staticmethod
    def _validate_workspace_path_no_config_refs(*configs: Mapping[str, Any]) -> None:
        """Reject ``${...}`` in ``WORKSPACE.path`` (must not reference other config keys)."""
        for config in configs:
            if not isinstance(config, Mapping):
                continue
            ws = config.get("WORKSPACE")
            if not isinstance(ws, Mapping):
                continue
            raw = ws.get("path")
            if raw is None:
                continue
            text = str(raw)
            if "${" in text:
                raise ValueError(
                    "WORKSPACE.path must not use ${...} config references; "
                    "use a literal absolute path or ~/... instead."
                )

    @staticmethod
    def _validate_workspace_yaml_config(config: Mapping[str, Any]) -> None:
        """Validate native workspace backend, path, and allow-path settings after YAML load."""
        ws = config.get("WORKSPACE")
        if not isinstance(ws, Mapping):
            return

        backend = str(ws.get("backend", "filesystem") or "filesystem").strip().lower()
        backend_aliases = {"filesystem", "filesystembackend", "state", "statebackend"}
        if backend not in backend_aliases:
            allowed = ", ".join(sorted(backend_aliases))
            raise ValueError(f"WORKSPACE.backend must be one of: {allowed}.")

        pv = ws.get("path")
        if pv is not None:
            raw = str(pv).strip()
            if raw and backend in {"state", "statebackend"}:
                raise ValueError("WORKSPACE.path cannot be used with WORKSPACE.backend: state.")
            if raw and not Path(raw).expanduser().is_absolute():
                raise ValueError(
                    "WORKSPACE.path must be an absolute path (or ~/...); relative paths are not allowed in YAML."
                )

        ap = ws.get("allow_path")
        if ap is None:
            return
        if isinstance(ap, (str, bytes)):
            raise ValueError("WORKSPACE.allow_path must be a list of absolute path strings, not a single string.")
        if not isinstance(ap, Sequence):
            raise ValueError("WORKSPACE.allow_path must be a list of absolute path strings.")
        for item in ap:
            s = str(item).strip()
            if s and not Path(s).expanduser().is_absolute():
                raise ValueError(
                    f"WORKSPACE.allow_path entries must be absolute paths; relative path not allowed: {s!r}"
                )

    @staticmethod
    def _normalize_subagent_alias(config: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize deprecated ``SUBAGENT_CONFIGS`` into the canonical ``SUBAGENTS`` key."""
        result = copy.deepcopy(dict(config))
        if "SUBAGENT_CONFIGS" not in result:
            raw_override_keys = result.get("OVERRIDE_KEYS")
            if isinstance(raw_override_keys, list):
                result.update(
                    {
                        "OVERRIDE_KEYS": [
                            "SUBAGENTS" if str(item).strip() == "SUBAGENT_CONFIGS" else item
                            for item in raw_override_keys
                        ]
                    }
                )
            return result
        if "SUBAGENTS" in result:
            raise ValueError("Use only SUBAGENTS; SUBAGENT_CONFIGS is a deprecated alias and cannot appear with it.")
        result.update({"SUBAGENTS": result.pop("SUBAGENT_CONFIGS")})
        raw_override_keys = result.get("OVERRIDE_KEYS")
        if isinstance(raw_override_keys, list):
            result.update(
                {
                    "OVERRIDE_KEYS": [
                        "SUBAGENTS" if str(item).strip() == "SUBAGENT_CONFIGS" else item for item in raw_override_keys
                    ]
                }
            )
        return result

    @staticmethod
    def _validate_workspace_policy_layout(config: Mapping[str, Any]) -> None:
        """Validate ``WORKSPACE_POLICY.layout`` segment paths after YAML load."""
        policy = config.get("WORKSPACE_POLICY")
        if not isinstance(policy, Mapping):
            return
        layout = policy.get("layout")
        if not isinstance(layout, Mapping):
            return
        for key, value in layout.items():
            if key not in DEFAULT_WORKSPACE_LAYOUT:
                continue
            if value is None:
                raise ValueError(f"WORKSPACE_POLICY.layout.{key} must be a non-empty relative path segment.")
            raw = str(value).strip()
            if not raw:
                raise ValueError(f"WORKSPACE_POLICY.layout.{key} must be a non-empty relative path segment.")
            segment_path = Path(raw)
            if segment_path.is_absolute():
                raise ValueError(
                    f"WORKSPACE_POLICY.layout.{key} must be a relative path segment; "
                    f"absolute paths are not allowed: {raw!r}"
                )
            if ".." in segment_path.parts:
                raise ValueError(f"WORKSPACE_POLICY.layout.{key} must not contain '..'; got: {raw!r}")

    @staticmethod
    def _get_raw_value_from(config: Mapping[str, Any], key: str) -> Any:
        """
        Resolve a dotted configuration path against a mapping root.

        Used during ``reload()`` interpolation so ``${...}`` references resolve
        against the in-flight ``working`` config, not stale ``self.settings``.

        Args:
            config: Configuration mapping root (e.g. pre-merge ``working`` dict).
            key: Dotted path such as ``MODEL.chat_model.params.model``.

        Returns:
            Resolved value, or ``None`` when any segment is missing.
        """
        value: Any = config
        for segment in key.split("."):
            if isinstance(value, Mapping) and segment in value:
                value = value[segment]
            else:
                return None
        return value

    def copy(self) -> "ConfigManager":
        """deep copy of a config"""

        new_config = ConfigManager()
        new_config.config_path = self.config_path
        new_config.settings = self.get_all()
        new_config.activated_suites = list(self.activated_suites)
        return new_config

    def interpolate_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Return a deep-copied config mapping with variable interpolation applied."""
        result = copy.deepcopy(dict(config))
        self._process_interpolation(result)
        return result

    def reload(self, config_path: str, default_config_path: str | None = None) -> None:
        """Reload effective native configuration, including any requested Suites."""
        self.reload_native(config_path, default_config_path=default_config_path)

    def reload_native(self, config_path: str, default_config_path: str | None = None) -> None:
        """Resolve Suite bundles into configuration layers before native compilation.

        The resulting ``settings`` mapping contains only normal DataAgent
        configuration sections. Suite filesystem layout and activation metadata
        stay within this configuration-loading boundary and never reach the
        Deep Agents compiler.
        """
        from dataagent.config.suite_resolver import SuiteConfigResolver
        from dataagent.core.suite.merge import (
            apply_override_keys,
            extract_user_layer,
            merge_layers,
            parse_override_keys,
        )
        from dataagent.utils.constants import META_OVERRIDE_KEYS

        with self._lock:
            default_config = self._load_yaml_mapping(default_config_path) if default_config_path else {}
            user_config = self._load_yaml_mapping(config_path)
            default_config = self._normalize_subagent_alias(default_config)
            user_config = self._normalize_subagent_alias(user_config)
            self._validate_workspace_path_no_config_refs(user_config, default_config)

            working = copy.deepcopy(default_config)
            self._deep_merge(working, user_config)
            self._process_interpolation(working)

            override_keys = parse_override_keys(user_config)
            user_layer = extract_user_layer(working, user_config)
            user_layer.pop(META_OVERRIDE_KEYS, None)

            suite_layers: tuple[dict[str, Any], ...] = ()
            activated_suites: tuple[dict[str, str], ...] = ()
            if "SUITE" in user_config:
                suite_config = user_layer.get("SUITE")
                if suite_config is None:
                    suite_config = user_config.get("SUITE")
                resolution = SuiteConfigResolver().resolve(suite_config if isinstance(suite_config, Mapping) else None)
                suite_layers = resolution.layers
                activated_suites = resolution.activated_suites
                logger.debug("Activated native Suites: {}", [entry.get("name") for entry in activated_suites])

            result = merge_layers([default_config, *suite_layers, user_layer])
            result.pop("SUITE", None)
            apply_override_keys(result, user_layer, override_keys)
            result.pop(META_OVERRIDE_KEYS, None)
            self._process_interpolation(result)
            self._validate_workspace_yaml_config(result)
            self._validate_workspace_policy_layout(result)

            self.config_path = Path(config_path).resolve()
            self.settings = result
            self.activated_suites = list(activated_suites)
            self.last_reload = datetime.now(timezone(timedelta(hours=8)))

    def get_all(self) -> dict:
        """
        返回所有当前配置（深拷贝，防止外部修改）
        """
        with self._lock:
            return copy.deepcopy(self.settings)

    def set(self, key: str, value: Any) -> None:
        """
        Set configuration value (runtime, private method)

        Args:
            key (str): Configuration key
            value (Any): Configuration value
        """
        with self._lock:
            keys = key.split(".")
            target = self.settings

            # Navigate to target location
            for k in keys[:-1]:
                if k not in target:
                    target[k] = {}
                target = target[k]

            # Set value
            target[keys[-1]] = value

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value (private method)

        Args:
            key (str): Configuration key, supports dot-separated nested keys like "database.host"
            default (Any): Default value

        Returns:
            Configuration value
        """
        with self._lock:
            # Get from configuration
            keys = key.split(".")
            value = self.settings

            for k in keys:
                if isinstance(value, dict) and k in value:
                    value = value[k]
                else:
                    return default

            return value

    def update(self, new_config: dict[str, Any]):
        """
        update config
        """

        with self._lock:
            self.settings.update(new_config)

    def _deep_merge(self, target: dict, source: dict) -> None:
        """Deep merge two dictionaries"""
        for key, value in source.items():
            if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                self._deep_merge(target[key], value)
            else:
                target[key] = value

    @staticmethod
    def _load_yaml_mapping(config_path: str) -> dict[str, Any]:
        path = Path(config_path)
        try:
            with open(path, encoding="utf-8") as file:
                loaded = yaml.safe_load(file) or {}
        except Exception as exc:
            raise RuntimeError(f"Failed to load configuration file {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"Configuration root must be a mapping: {path}")
        logger.trace(f"Loaded basic configuration file: {path}")
        return copy.deepcopy(loaded)

    def _process_interpolation(self, config_dict: dict) -> None:
        """Process variable interpolation in configuration values"""

        def resolve_env_ref(value: str, path: str) -> str:
            pattern = r"\$env\{([^}]+)\}"
            matches = re.findall(pattern, value)
            if not matches:
                return value

            result = value
            for var_name in matches:
                env_value = os.getenv(var_name)
                if env_value is None:
                    raise ValueError(
                        f"环境变量 '{var_name}' 未设置。请在 .env 文件中设置: {var_name}=\"your_value\" (path: {path})"
                    )
                result = result.replace(f"$env{{{var_name}}}", env_value)
            return result

        def interpolate_value(value, path=""):
            if isinstance(value, str):
                # Resolve explicit env references first
                value = resolve_env_ref(value, path)
                # Find all ${...} patterns
                pattern = r"\$\{([^}]+)\}"
                matches = re.findall(pattern, value)

                if matches:
                    result = value
                    for match in matches:
                        # Get the referenced value
                        ref_value = self._get_raw_value_from(config_dict, match)
                        if ref_value is not None:
                            result = result.replace(f"${{{match}}}", str(ref_value))
                        else:
                            logger.warning(f"Variable reference '${{{match}}}' not found in config path: {path}")
                    return result
                return value
            if isinstance(value, dict):
                for k, v in value.items():
                    value[k] = interpolate_value(v, f"{path}.{k}" if path else k)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    value[i] = interpolate_value(item, f"{path}[{i}]" if path else f"[{i}]")
            return value

        try:
            result = interpolate_value(config_dict)
            if result is None:
                logger.warning("Variable interpolation returned None")
        except Exception as e:
            logger.error(f"Failed to process variable interpolation: {e}")
            raise

    def _get_raw_value(self, key: str) -> Any:
        """Get raw configuration value from committed ``self.settings``."""
        return self._get_raw_value_from(self.settings, key)
