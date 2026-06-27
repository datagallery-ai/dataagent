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
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

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
    def _validate_workspace_yaml_config(config: Mapping[str, Any]) -> None:
        """Validate ``WORKSPACE.path`` / ``WORKSPACE.allow_path`` after YAML load (non-empty → absolute, ``~/`` OK)."""
        ws = config.get("WORKSPACE")
        if not isinstance(ws, Mapping):
            return

        pv = ws.get("path")
        if pv is not None:
            if isinstance(pv, Sequence) and not isinstance(pv, (str, bytes)):
                raise ValueError(
                    "WORKSPACE.path must be a single absolute path string; multiple workspace roots are not supported."
                )
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

    def copy(self) -> "ConfigManager":
        """deep copy of a config"""

        new_config = ConfigManager()
        new_config.config_path = self.config_path
        new_config.settings = self.get_all()
        new_config.activated_suites = list(self.activated_suites)
        return new_config

    def merge_configs(self, base_config, override_config):
        """Public method to merge two configurations"""
        merged = {}
        self._deep_merge(merged, base_config)
        self._deep_merge(merged, override_config)
        return merged

    def reload(self, config_path: str, default_config_path: str | None = None) -> None:
        """
        Reload configuration.

        加载顺序：
        1. 默认配置文件 default_config_path
        2. 必选：用户指定的主配置文件 config_path
        3. 处理变量插值
        """
        with self._lock:
            self.settings.clear()
            self.activated_suites = []
            default_config: dict[str, Any] = {}
            file_config: dict[str, Any] = {}

            # 1. 先加载默认配置
            if default_config_path:
                default_yaml = Path(default_config_path)
                try:
                    with open(default_yaml, encoding="utf-8") as f:
                        default_config = yaml.safe_load(f) or {}
                    self._deep_merge(self.settings, default_config)
                    logger.trace(f"Loaded default configuration file: {default_yaml}")
                except Exception as e:
                    raise RuntimeError(f"Failed to load default configuration file {default_yaml}") from e
            else:
                logger.warning(
                    "No default configuration file provided, may cause critical errors if using REACT type agent"
                )

            # 2. 用户主配置（覆盖默认配置）
            yaml_file = Path(config_path)
            self.config_path = yaml_file.resolve()
            try:
                with open(yaml_file, encoding="utf-8") as f:
                    file_config = yaml.safe_load(f) or {}

                # Merge directly into settings
                self._deep_merge(self.settings, file_config)
                logger.trace(f"Loaded configuration file: {yaml_file}")

            except Exception as e:
                # 如果找不到配置文件则直接抛出异常，避免继续执行
                raise RuntimeError(f"Failed to load configuration file {yaml_file}: {e}") from e

            # 3. 变量插值，允许在yaml文件中用 ${...} 引用其他配置项，支持 $env{VAR} 引用环境变量
            self._process_interpolation(self.settings)
            self._apply_suite_layers(default_config=default_config, file_config=file_config)
            # 5. WORKSPACE.path / allow_path 在启动前校验（非空则须为绝对路径）
            self._validate_workspace_yaml_config(self.settings)
            self.last_reload = datetime.now(timezone(timedelta(hours=8)))  # 东八区

    def get_all(self) -> dict:
        """
        返回所有当前配置（深拷贝，防止外部修改）
        """
        import copy

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
        """Return the root path for one activated Suite."""
        from dataagent.suite.activated_suites import resolve_activated_suite_root

        return resolve_activated_suite_root(suite_name, self.activated_suites)

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

    def _apply_suite_layers(self, *, default_config: dict[str, Any], file_config: dict[str, Any]) -> None:
        """Apply Suite layers when the user YAML explicitly includes suites."""
        suite_config = file_config.get("SUITE")
        if not isinstance(suite_config, Mapping) or not suite_config.get("include"):
            return

        from dataagent.suite.activation import activate_suites, order_suites_for_merge
        from dataagent.suite.discovery import discover_suite_index
        from dataagent.suite.merge import extract_user_layer, merge_layers
        from dataagent.suite.suite_layer import build_suite_layers
        from dataagent.suite.validation import validate_merged_config

        default_actor_nodes = {
            str(item.get("node") or "").strip()
            for item in default_config.get("ACTOR_LOOP", [])
            if isinstance(item, Mapping) and str(item.get("node") or "").strip()
        }
        index = discover_suite_index(config=self.settings)
        activated = activate_suites(suite_config=suite_config, index=index)
        suite_layers, activated_suites = build_suite_layers(
            order_suites_for_merge(activated),
            default_actor_nodes=default_actor_nodes,
        )
        user_layer = extract_user_layer(self.settings, file_config)
        self.settings = merge_layers([default_config, *suite_layers, user_layer])
        self.activated_suites = activated_suites
        self._process_interpolation(self.settings)
        validate_merged_config(self.settings, activated_suites=self.activated_suites)

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
                        ref_value = self._get_raw_value(match)
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
        """Get raw configuration value without environment variable override"""
        keys = key.split(".")
        value = self.settings

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return None

        return value
