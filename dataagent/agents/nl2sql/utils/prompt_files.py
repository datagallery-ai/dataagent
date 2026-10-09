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
from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from dataagent.utils.constants import NL2SQL_PROMPT_PREFIX
from dataagent.utils.runtime_paths import dataagent_package_path


@dataclass(frozen=True)
class EditablePromptSpec:
    document: str
    config_key: str
    package_dir: str


#: 唯一的开放清单：文档名 → 解析规则。新增可在线修改的 md 文档在这里登记。
EDITABLE_PROMPTS: dict[str, EditablePromptSpec] = {
    "sql_rules": EditablePromptSpec(
        document="sql_rules",
        config_key="CORE.perceptor.user_sql_rules",
        package_dir="user",
    ),
}

#: REST 当前不传文档名，固定操作默认文档。
DEFAULT_EDITABLE_PROMPT = "sql_rules"


def editable_prompt_path(document: str, yaml_path: Path) -> Path:
    """按文档名和配置文件路径，返回可编辑 md 文件的绝对路径"""
    spec = EDITABLE_PROMPTS.get(document)
    if spec is None:
        raise ValueError(f"unknown editable prompt document: {document!r}")
    with yaml_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {yaml_path}")
    raw = _config_value(data, spec.config_key)
    name = str(raw or "").strip()
    if not name:
        raise ValueError(f"{spec.config_key} is not configured: {yaml_path}")
    if name != Path(name).name:
        raise ValueError(f"{spec.config_key} must be a bare file name, got: {raw!r}")
    if not name.endswith(".md"):
        name = f"{name}.md"
    parts = (*NL2SQL_PROMPT_PREFIX.split("/"), *spec.package_dir.split("/"), name)
    return dataagent_package_path(*parts)


def read_prompt_file(path: Path) -> tuple[str, str]:
    """读取可编辑 md 文件内容，返回 (markdown, mtime_iso)"""
    markdown = path.read_text(encoding="utf-8")
    return markdown, _mtime_to_iso(path)


def write_prompt_file(path: Path, markdown: str) -> str:
    """写入可编辑 md 文件内容，返回 mtime_iso"""
    if not path.is_file():
        raise FileNotFoundError(f"prompt file not found: {path}")
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(markdown)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return _mtime_to_iso(path)


def _config_value(data: Mapping[str, Any], dotted_key: str) -> Any:
    """按点路径取嵌套配置值；任一层缺失返回 None"""
    value: Any = data
    for part in dotted_key.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _mtime_to_iso(path: Path) -> str:
    """mtime → UTC ISO 8601 字符串"""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
