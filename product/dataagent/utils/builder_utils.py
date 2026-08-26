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
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

from dataagent.utils.runtime_paths import dataagent_home, dataagent_package_path

_AGENT_PRESET_PATHS = {
    "deep_analyze": ("core", "flex", "examples", "deep_analyze.yaml"),
}


def _require_supported_agent_type(agent_type: Any, *, source: str | Path) -> str:
    """Return a builder preset name or raise a configuration error."""
    if not isinstance(agent_type, str):
        raise ValueError(
            f"Failed to resolve `agent_type` from config: {source}. "
            "Please ensure `AGENT_CONFIG.agent_type` is a string."
        )
    if agent_type not in _AGENT_PRESET_PATHS:
        supported_values = ", ".join(sorted(_AGENT_PRESET_PATHS))
        raise ValueError(
            f"Unsupported `agent_type`: {agent_type!r} from config: {source}. Supported values are: {supported_values}."
        )
    return agent_type


def get_agent_type(config: Any, *, source: str | Path) -> str:
    """从配置中提取 agent_type。"""
    try:
        # agent_type 区别于现存 YAML 配置文件中的 type。agent_type 可选值：deep_analyze
        agent_type = config["AGENT_CONFIG"]["agent_type"]
    except (TypeError, KeyError):
        raise ValueError(
            f"Failed to resolve `agent_type` from config: {source}. Please ensure `AGENT_CONFIG.agent_type` exists."
        ) from None
    return _require_supported_agent_type(agent_type, source=source)


def resolve_example_yaml_path(*, agent_type: str) -> Path:
    """按 agent_type 返回预制 Agent 示例 YAML 的源路径。"""
    agent_type = _require_supported_agent_type(agent_type, source="agent_type")
    return dataagent_package_path(*_AGENT_PRESET_PATHS[agent_type])


def _output_path(output_dir: Path, filename: str) -> Path:
    output_dir = output_dir.resolve()
    path = (output_dir / filename).resolve()
    if not path.is_relative_to(output_dir):
        raise ValueError(f"Builder output path escapes the output directory: {filename!r}.")
    return path


def get_original_yaml_path(*, agent_type: str, output_dir: Path) -> Path:
    """按 agent_type 生成 original YAML 文件并返回路径。"""
    example_config_path = resolve_example_yaml_path(agent_type=agent_type)

    original_yaml_path = _output_path(output_dir, f"original_{agent_type}_config.yaml")
    if original_yaml_path.exists():
        with suppress(OSError):
            original_yaml_path.unlink()
    shutil.copy2(example_config_path, original_yaml_path)
    return original_yaml_path


def write_temp_yaml_path(*, agent_type: str, config_dict: dict, output_dir: Path) -> Path:
    """生成 temp YAML 文件并返回路径。"""
    temp_yaml_path = _output_path(output_dir, f"temp_{agent_type}_config.yaml")
    if temp_yaml_path.exists():
        with suppress(OSError):
            temp_yaml_path.unlink()
    with temp_yaml_path.open("w", encoding="utf-8") as temp_file:
        yaml.safe_dump(config_dict, temp_file, allow_unicode=True, sort_keys=False)
    return temp_yaml_path


def merge_yaml_cfg_val(original_value: Any, new_value: Any) -> Any:
    """递归合并 YAML 配置。

    合并规则（相同 key 追加字段）：
    - dict + dict：递归合并；同 key 继续向下追加
    - list + list：列表拼接追加
    - 其他类型：使用 temp(new_value) 的值覆盖 original(original_value)
    """
    if isinstance(original_value, dict) and isinstance(new_value, dict):
        merged_value = dict(original_value)
        for key, value in new_value.items():
            if key in merged_value:
                merged_value[key] = merge_yaml_cfg_val(merged_value[key], value)
            else:
                merged_value[key] = value
        return merged_value
    if isinstance(original_value, list) and isinstance(new_value, list):
        return [*original_value, *new_value]
    return new_value


def replace_set_scenario_fields_with_original_scenario(
    *,
    original_config: dict[str, Any],
    temp_config: dict[str, Any],
) -> dict[str, Any]:
    """按增量方式将 set_scenario 追加到 original SCENARIO。"""
    temp_scenario = temp_config.get("SCENARIO", {})
    if temp_scenario is None:
        temp_scenario = {}
    if not isinstance(temp_scenario, dict):
        return temp_config

    temp_chat = temp_scenario.get("chat", {})
    if temp_chat is None:
        temp_chat = {}
    if not isinstance(temp_chat, dict):
        return temp_config

    original_scenario = original_config.get("SCENARIO", {})
    if original_scenario is None:
        original_scenario = {}
    if not isinstance(original_scenario, dict):
        original_scenario = {}

    original_chat = original_scenario.get("chat", {})
    if original_chat is None:
        original_chat = {}
    if not isinstance(original_chat, dict):
        original_chat = {}

    merged_scenario = merge_yaml_cfg_val(original_scenario, temp_scenario)
    if not isinstance(merged_scenario, dict):
        return temp_config
    merged_chat = merged_scenario.get("chat", {})
    if merged_chat is None:
        merged_chat = {}
    if not isinstance(merged_chat, dict):
        merged_chat = {}

    for field_name in ("instructions", "constraints"):
        temp_value = temp_chat.get(field_name)
        if not isinstance(temp_value, str) or not temp_value:
            continue
        original_value = original_chat.get(field_name)
        if isinstance(original_value, str) and original_value:
            merged_chat[field_name] = f"{original_value}{temp_value}"
        else:
            merged_chat[field_name] = temp_value

    merged_scenario["chat"] = merged_chat
    updated_temp_config = dict(temp_config)
    updated_temp_config["SCENARIO"] = merged_scenario
    return updated_temp_config


def get_merge_yaml_path(
    *,
    agent_type: str,
    output_dir: Path,
    original_yaml_path: Path,
    temp_yaml_path: Path,
) -> Path:
    """合并 original/temp YAML 文件，生成 merged YAML 并返回路径。"""
    merged_yaml_path = _output_path(output_dir, f"merged_{agent_type}_config.yaml")
    if merged_yaml_path.exists():
        with suppress(OSError):
            merged_yaml_path.unlink()

    original_config: Any = {}
    if original_yaml_path.exists():
        with original_yaml_path.open("r", encoding="utf-8") as original_file:
            original_config = yaml.safe_load(original_file) or {}

    with temp_yaml_path.open("r", encoding="utf-8") as temp_file:
        temp_config = yaml.safe_load(temp_file) or {}

    if isinstance(original_config, dict) and isinstance(temp_config, dict):
        # 将 set_scenario 增量追加到 original SCENARIO。
        temp_config = replace_set_scenario_fields_with_original_scenario(
            original_config=original_config,
            temp_config=temp_config,
        )

    # 合并 original/temp YAML 配置。
    merged_config = merge_yaml_cfg_val(original_config, temp_config)
    with merged_yaml_path.open("w", encoding="utf-8") as merged_file:
        yaml.safe_dump(merged_config, merged_file, allow_unicode=True, sort_keys=False)
    return merged_yaml_path


def remove_sensitive_info_of_output_yamls(*, output_dir: Path) -> None:
    """将 output 目录下 YAML 文件内容脱敏后覆盖写回。"""

    def _mask_sensitive_fields(data: Any) -> Any:
        if isinstance(data, dict):
            masked_data: dict[Any, Any] = {}
            for key, value in data.items():
                if key in {"base_url", "api_key"}:
                    if isinstance(value, str):
                        if value:
                            masked_data[key] = "*" * len(value)
                        else:
                            masked_data[key] = "******"
                    else:
                        masked_data[key] = "******"
                else:
                    masked_data[key] = _mask_sensitive_fields(value)
            return masked_data
        if isinstance(data, list):
            masked_list: list[Any] = []
            for item in data:
                masked_list.append(_mask_sensitive_fields(item))
            return masked_list
        return data

    for yaml_file_path in sorted(output_dir.glob("*.yaml")):
        try:
            with yaml_file_path.open("r", encoding="utf-8") as yaml_file:
                yaml_content = yaml.safe_load(yaml_file) or {}
        except (OSError, yaml.YAMLError):
            continue
        masked_yaml_content = _mask_sensitive_fields(yaml_content)
        try:
            with yaml_file_path.open("w", encoding="utf-8") as yaml_file:
                yaml.safe_dump(masked_yaml_content, yaml_file, allow_unicode=True, sort_keys=False)
        except (OSError, yaml.YAMLError):
            continue


def get_final_yaml(*, agent_type: str, config_dict: dict) -> Path:
    """在 ``~/.dataagent/.builder/output`` 下生成 original/temp/merged YAML，并返回 merged 路径供加载。"""
    agent_type = _require_supported_agent_type(agent_type, source="config_dict")
    output_dir = dataagent_home() / ".builder" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1) 从包内示例复制到 output 目录，作为合并基底
        original_yaml_path = get_original_yaml_path(
            agent_type=agent_type,
            output_dir=output_dir,
        )
        # 2) 将本次构建参数写入临时 YAML，作为增量配置输入
        temp_yaml_path = write_temp_yaml_path(
            agent_type=agent_type,
            config_dict=config_dict,
            output_dir=output_dir,
        )
        # 3) 合并原始配置与临时配置，生成 merged YAML（仅存在于 output 目录，不写回包内示例）
        merged_yaml_path = get_merge_yaml_path(
            agent_type=agent_type,
            output_dir=output_dir,
            original_yaml_path=original_yaml_path,
            temp_yaml_path=temp_yaml_path,
        )
        return merged_yaml_path
    finally:
        # 无论 get_final_yaml 是否成功，output 目录下已经生成的 YAML 都会被统一脱敏后覆盖写回
        remove_sensitive_info_of_output_yamls(output_dir=output_dir)


def normalize_actions(
    value: list[dict[str, Any]] | None,
    field_name: str,
) -> list[dict[str, Any]] | None:
    """规范化 TOOLS 下列表型 action 配置，并做字段校验。"""
    required_fields_map: dict[str, tuple[str, ...]] = {
        "skills": ("user", "builtin"),
    }
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"`{field_name}` must be a list[dict] or None.")
    if field_name not in required_fields_map:
        raise ValueError(f"Unsupported action field: {field_name!r}.")

    required_fields = required_fields_map[field_name]
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"`{field_name}[{index}]` must be a dict.")

        normalized_item = dict(item)
        for required_field in required_fields:
            field_value = normalized_item.get(required_field)
            if not isinstance(field_value, str) or not field_value.strip():
                raise ValueError(f"`{field_name}[{index}].{required_field}` must be a non-empty string.")

        if field_name == "mcp":
            config = normalized_item.get("config")
            if config is None:
                normalized_item["config"] = {}
            elif not isinstance(config, dict):
                raise ValueError(f"`{field_name}[{index}].config` must be a dict.")

        normalized_items.append(normalized_item)

    return normalized_items


def normalize_skill_allowlists(value: list[str] | None) -> dict[str, list[str]] | None:
    """规范化 TOOLS.skills allowlist 配置。"""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("`skills` must be a list[str] or None.")
    res = {"builtin": [], "user": []}
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"`skills[{name}]` must be a non-empty string.")
        res["user"].append(name.strip())
    return res
