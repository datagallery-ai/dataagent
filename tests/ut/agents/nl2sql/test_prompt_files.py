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
"""prompt_files：开放清单、路径解析与原子读写的单元测试。

包路径解析通过 monkeypatch 重定向到临时目录，不触碰真实安装目录。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from dataagent.agents.nl2sql.utils import prompt_files as prompt_files_module
from dataagent.agents.nl2sql.utils.prompt_files import (
    DEFAULT_EDITABLE_PROMPT,
    editable_prompt_path,
    read_prompt_file,
    write_prompt_file,
)


@pytest.fixture
def package_prompts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect package prompt resolution into a temp package tree."""
    prompts_dir = tmp_path / "package" / "agents/nl2sql/prompts/user"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "sql_rules_demo.md").write_text("# demo rules\n", encoding="utf-8")
    monkeypatch.setattr(
        prompt_files_module, "dataagent_package_path", lambda *parts: tmp_path.joinpath("package", *parts)
    )
    return prompts_dir


def _write_scenario_yaml(tmp_path: Path, config: dict[str, Any]) -> Path:
    yaml_path = tmp_path / "scenario.yaml"
    yaml_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return yaml_path


def test_default_editable_prompt_is_registered() -> None:
    """默认文档名必须已在开放清单里登记。"""
    assert DEFAULT_EDITABLE_PROMPT in prompt_files_module.EDITABLE_PROMPTS
    assert prompt_files_module.EDITABLE_PROMPTS[DEFAULT_EDITABLE_PROMPT].config_key == "CORE.perceptor.user_sql_rules"


def test_editable_prompt_path_appends_md_suffix(tmp_path: Path, package_prompts_dir: Path) -> None:
    """yaml 配置不带 .md 后缀时自动补齐，落到包内 prompts/user/ 下。"""
    yaml_path = _write_scenario_yaml(tmp_path, {"CORE": {"perceptor": {"user_sql_rules": "sql_rules_demo"}}})

    path = editable_prompt_path(DEFAULT_EDITABLE_PROMPT, yaml_path)

    assert path == package_prompts_dir / "sql_rules_demo.md"


def test_editable_prompt_path_keeps_md_suffix(tmp_path: Path, package_prompts_dir: Path) -> None:
    """yaml 配置已带 .md 后缀时保持原样。"""
    yaml_path = _write_scenario_yaml(tmp_path, {"CORE": {"perceptor": {"user_sql_rules": "sql_rules_demo.md"}}})

    path = editable_prompt_path(DEFAULT_EDITABLE_PROMPT, yaml_path)

    assert path == package_prompts_dir / "sql_rules_demo.md"


def test_editable_prompt_path_rejects_unregistered_document(tmp_path: Path) -> None:
    """未登记的文档名单点拒绝。"""
    yaml_path = _write_scenario_yaml(tmp_path, {})

    with pytest.raises(ValueError, match="unknown editable prompt document"):
        editable_prompt_path("no_such_document", yaml_path)


def test_editable_prompt_path_rejects_missing_config(tmp_path: Path) -> None:
    """场景 yaml 没配目标文件名时报错。"""
    yaml_path = _write_scenario_yaml(tmp_path, {"CORE": {"perceptor": {}}})

    with pytest.raises(ValueError, match="CORE.perceptor.user_sql_rules is not configured"):
        editable_prompt_path(DEFAULT_EDITABLE_PROMPT, yaml_path)


@pytest.mark.parametrize("bad_name", ["../sql_rules_demo", "a/b", "/tmp/sql_rules_demo", "."])
def test_editable_prompt_path_rejects_non_bare_file_name(tmp_path: Path, bad_name: str) -> None:
    """配置带目录或路径穿越时拒绝，写入目标始终固定在包内目录。"""
    yaml_path = _write_scenario_yaml(tmp_path, {"CORE": {"perceptor": {"user_sql_rules": bad_name}}})

    with pytest.raises(ValueError, match="must be a bare file name"):
        editable_prompt_path(DEFAULT_EDITABLE_PROMPT, yaml_path)


def test_read_prompt_file_returns_markdown_and_mtime(package_prompts_dir: Path) -> None:
    path = package_prompts_dir / "sql_rules_demo.md"

    markdown, updated_at = read_prompt_file(path)

    assert markdown == "# demo rules\n"
    assert updated_at == datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def test_write_prompt_file_replaces_whole_file(package_prompts_dir: Path) -> None:
    path = package_prompts_dir / "sql_rules_demo.md"

    updated_at = write_prompt_file(path, "# new rules\nline2")

    assert path.read_text(encoding="utf-8") == "# new rules\nline2"
    assert updated_at == datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    # 原子写不留临时文件
    assert not list(package_prompts_dir.glob(".*.tmp"))


def test_write_prompt_file_rejects_missing_target(package_prompts_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        write_prompt_file(package_prompts_dir / "sql_rules_missing.md", "# rules")
    # 目标不存在时不创建新文件
    assert not (package_prompts_dir / "sql_rules_missing.md").exists()
