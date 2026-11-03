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

import contextlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from dataagent.utils.constants import NL2SQL_PROMPT_PREFIX
from dataagent.utils.env_utils import get_env
from dataagent.utils.runtime_paths import dataagent_package_path

#: 在线修改的目录。未设置时，view、update、查询都只用包内种子。
PROMPT_DIR_ENV = "DATAAGENT_PROMPT_DIR"


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


def _prompt_dir() -> Path | None:
    raw = (get_env(PROMPT_DIR_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def persisted_prompt_file(file_name: str) -> Path | None:
    """环境变量目录里已有的同名文件。未设置、名字不是裸文件名、或文件不存在时返回 None。"""
    root = _prompt_dir()
    if root is None or not file_name or file_name in {".", ".."} or file_name != Path(file_name).name:
        return None
    path = root / file_name
    return path if path.is_file() else None


def read_prompt_file(path: Path) -> tuple[str, str]:
    """读取可编辑 md 文件内容，返回 (markdown, mtime_iso)"""
    markdown = path.read_text(encoding="utf-8")
    return markdown, _mtime_to_iso(path)


def read_effective_prompt_file(package_path: Path) -> tuple[str, str]:
    """优先读环境变量目录中的修改，否则读包内种子。"""
    return read_prompt_file(persisted_prompt_file(package_path.name) or package_path)


def write_prompt_file(path: Path, markdown: str) -> str:
    """写入可编辑 md 文件内容，返回 mtime_iso"""
    if not path.is_file():
        raise FileNotFoundError(f"prompt file not found: {path}")
    return _atomic_write(path, markdown)


def write_prompt_override(package_path: Path, markdown: str) -> str:
    """有 ``DATAAGENT_PROMPT_DIR`` 时写到该目录，否则改包内种子。包内种子必须已存在。"""
    if not package_path.is_file():
        raise FileNotFoundError(f"prompt file not found: {package_path}")
    root = _prompt_dir()
    if root is None:
        return write_prompt_file(package_path, markdown)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    return _atomic_write(root / package_path.name, markdown)


def _atomic_write(path: Path, markdown: str) -> str:
    """同目录临时文件原子替换，返回新的 mtime。临时文件在返回前删除。"""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(markdown)
        os.replace(tmp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_name)
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
