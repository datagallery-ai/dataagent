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
else:
    logger.warning(f".env file not found at: {env_path}")


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
        ``SUBAGENT_CONFIGS``, workflow lists, etc.) are **appended** with the override
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

        override = override_config or {}
        override_keys = parse_override_keys(override)
        user_layer = copy.deepcopy(override)
        user_layer.pop(META_OVERRIDE_KEYS, None)
        result = merge_layers([base_config or {}, user_layer])
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
        """Validate ``WORKSPACE.path`` / ``WORKSPACE.allow_path`` after YAML load (non-empty → absolute, ``~/`` OK)."""
        ws = config.get("WORKSPACE")
        if not isinstance(ws, Mapping):
            return

        pv = ws.get("path")
        if pv is not None:
            raw = str(pv).strip()
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
    def _validate_swarm_yaml_config(config: Mapping[str, Any]) -> None:
        """Validate ``SWARM.worker_max_concurrent`` after YAML load.

        Only ``None``/omitted or a non-negative Python ``int`` is allowed.
        ``bool`` is rejected because it is a subclass of ``int`` in Python.
        Strings, floats, and negative integers are rejected.
        """
        swarm = config.get("SWARM")
        if not isinstance(swarm, Mapping):
            return
        raw = swarm.get("worker_max_concurrent")
        if raw is None:
            return
        if isinstance(raw, bool):
            raise ValueError(
                "SWARM.worker_max_concurrent must be a non-negative integer or omitted/null; "
                "boolean values are not allowed."
            )
        if isinstance(raw, int):
            if raw < 0:
                raise ValueError(f"SWARM.worker_max_concurrent must be non-negative, got {raw!r}.")
            return
        raise ValueError(
            "SWARM.worker_max_concurrent must be a non-negative integer or omitted/null; "
            f"got {type(raw).__name__}: {raw!r}."
        )

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
        """
        Reload configuration.

        1. Load default and user YAML (each deep-copied).
        2. Merge into ``tmp``, interpolate, validate workspace/swarm settings.
        3. Extract the user merge layer from user-written paths only.
        4. Discover and activate suites when ``SUITE`` is present.
        5. ``merge_layers([default, suite layers…, user layer])``, apply ``OVERRIDE_KEYS``, validate, assign settings.
        """
        with self._lock:
            from dataagent.core.suite.activation import activate_suites, order_suites_for_merge
            from dataagent.core.suite.discovery import discover_suite_index
            from dataagent.core.suite.merge import (
                apply_override_keys,
                extract_user_layer,
                merge_layers,
                parse_override_keys,
            )
            from dataagent.core.suite.suite_layer import build_suite_layers
            from dataagent.core.suite.validation import validate_merged_config
            from dataagent.utils.constants import META_OVERRIDE_KEYS

            default_config: dict[str, Any] = {}
            if default_config_path:
                default_yaml = Path(default_config_path)
                try:
                    with open(default_yaml, encoding="utf-8") as f:
                        loaded = yaml.safe_load(f) or {}
                    if isinstance(loaded, dict):
                        default_config = copy.deepcopy(loaded)
                    logger.trace(f"Loaded default configuration file: {default_yaml}")
                except Exception as e:
                    raise RuntimeError(f"Failed to load default configuration file {default_yaml}") from e
            else:
                logger.warning(
                    "No default configuration file provided, may cause critical errors if using REACT type agent"
                )

            yaml_file = Path(config_path)
            resolved_config_path = yaml_file.resolve()
            try:
                with open(yaml_file, encoding="utf-8") as f:
                    user_config = yaml.safe_load(f) or {}
                user_config = {} if not isinstance(user_config, dict) else copy.deepcopy(user_config)
                logger.trace(f"Loaded configuration file: {yaml_file}")
            except Exception as e:
                raise RuntimeError(f"Failed to load configuration file {yaml_file}: {e}") from e

            user_keys = set(user_config.keys())
            working: dict[str, Any] = copy.deepcopy(default_config)
            self._deep_merge(working, user_config)

            self._validate_workspace_path_no_config_refs(user_config, default_config)
            self._process_interpolation(working)
            self._validate_workspace_yaml_config(working)
            self._validate_swarm_yaml_config(working)

            override_keys = parse_override_keys(user_config)
            user_layer = extract_user_layer(working, user_config)
            user_layer.pop(META_OVERRIDE_KEYS, None)
            default_actor_nodes = {
                str(item.get("node")).strip()
                for item in default_config.get("ACTOR_LOOP", [])
                if isinstance(item, dict) and item.get("node")
            }

            suite_layers: list[dict[str, Any]] = []
            activated_meta: list[dict[str, str]] = []
            if "SUITE" in user_keys:
                suite_config = user_layer.get("SUITE")
                if suite_config is None:
                    suite_config = user_config.get("SUITE")
                index = discover_suite_index(config=working)
                activated = activate_suites(
                    suite_config=suite_config if isinstance(suite_config, dict) else None,
                    index=index,
                )
                suite_layers, activated_meta = build_suite_layers(
                    order_suites_for_merge(activated),
                    default_actor_nodes=default_actor_nodes,
                )
                logger.debug("Activated suites: {}", [m["name"] for m in activated_meta])

            layers = [default_config, *suite_layers, user_layer]
            result = merge_layers(layers)
            result.pop("SUITE", None)
            apply_override_keys(result, user_layer, override_keys)
            result.pop(META_OVERRIDE_KEYS, None)

            validate_merged_config(result, activated_suites=activated_meta)
            self._validate_workspace_policy_layout(result)

            self.config_path = resolved_config_path
            self.settings = result
            self.activated_suites = activated_meta
            self.last_reload = datetime.now(timezone(timedelta(hours=8)))  # 东八区

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

    def get_activated_suite_root(self, suite_name: str) -> Path:
        """
        Return the absolute root directory for one activated Suite.

        Args:
            suite_name: ``name`` from ``suite.yaml`` (e.g. ``ecommerce_suite``).

        Returns:
            Resolved absolute Suite root directory.

        Raises:
            ValueError: ``suite_name`` is empty, or the Suite is not activated.
        """
        from dataagent.core.suite.activated_suites import resolve_activated_suite_root

        with self._lock:
            return resolve_activated_suite_root(
                suite_name,
                activated_suites=self.activated_suites,
            )

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


def build_prompt(spec: dict[str, Any]) -> Any:
    """
    将 yaml ``prompt_template.<message_type>`` 的单条 spec 转为 ``PromptTemplate``。

    仅在 flex 加载节点配置时由 ``build_prompt_append`` / ``dataagent.core.flex.agent`` 调用。
    spec 必须恰好包含 ``path`` 或 ``content`` 之一（互斥）：

    - ``path``：**绝对路径**字符串（允许 ``~/...``，会先 ``expanduser``）；**不支持**相对路径。
    - ``content``：直接作为追加 prompt 正文（Jinja2 模板）。

    Returns:
        ``PromptTemplate`` 仅包含从文件或字符串读入的正文。
    """
    # 延迟导入，避免 ``dataagent.config`` 与 ``prompt_manager`` 在包初始化阶段形成环依赖。
    from dataagent.core.managers.prompt_manager.template import PromptTemplate

    if not isinstance(spec, dict):
        raise ValueError(f"prompt_template spec must be a mapping, got: {type(spec).__name__}")
    has_path = "path" in spec and spec["path"] is not None
    has_content = "content" in spec and spec["content"] is not None
    if has_path == has_content:
        raise ValueError(f"prompt_template spec must have exactly one of 'path' or 'content', got: {spec!r}")
    if has_path:
        path = Path(str(spec["path"]).strip()).expanduser()
        if not path.is_absolute():
            raise ValueError(
                "prompt_template 'path' must be an absolute path (or ~/...); "
                f"relative paths are not allowed: {spec['path']!r}"
            )
        return PromptTemplate.from_file(str(path))
    return PromptTemplate.from_string(spec["content"])


def build_prompt_append(spec_or_list: Any) -> Any:
    """
    将 ``prompt_template.<message_type>`` 的 YAML 值转为单个追加用 ``PromptTemplate``。

    支持单条 spec（dict）或多条 spec 列表（高优条目应排在列表前部）；多条时按顺序拼接正文，
    段间以空行分隔。

    Args:
        spec_or_list: 单条 ``{path|content}`` 映射，或非空 spec 列表。

    Returns:
        合并后的 ``PromptTemplate``，供 Planner ``prompt_appends`` 使用。
    """
    from dataagent.core.managers.prompt_manager.template import PromptTemplate

    if isinstance(spec_or_list, list):
        if not spec_or_list:
            raise ValueError("prompt_template message_type list must not be empty")
        parts = [build_prompt(item) for item in spec_or_list]
        combined = "\n\n".join(part.content for part in parts)
        return PromptTemplate.from_string(combined)
    if isinstance(spec_or_list, dict):
        return build_prompt(spec_or_list)
    raise ValueError(
        f"prompt_template message_type must be a mapping or list of mappings, got: {type(spec_or_list).__name__}"
    )
