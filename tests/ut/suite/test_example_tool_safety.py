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
from pathlib import Path

import pytest

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.suite.builtin_suites.example_suite.tools.example_tool import read_suite_doc


class _ConfigManager:
    def __init__(self, suite_root: Path):
        self._suite_root = suite_root

    def get_activated_suite_root(self, _suite_name: str) -> Path:
        return self._suite_root


def test_read_suite_doc_rejects_filename_that_escapes_custom_subdir(tmp_path: Path) -> None:
    suite_root = tmp_path / "suite"
    custom_dir = suite_root / "custom_dir"
    custom_dir.mkdir(parents=True)
    (suite_root / "suite.yaml").write_text("secret: true\n", encoding="utf-8")
    ctx = ToolExecutionContext(config_manager=_ConfigManager(suite_root))

    with pytest.raises(ValueError, match="filename"):
        read_suite_doc(filename="../suite.yaml", _tool_context=ctx)


def test_read_suite_doc_reads_file_inside_custom_subdir(tmp_path: Path) -> None:
    suite_root = tmp_path / "suite"
    custom_dir = suite_root / "custom_dir"
    custom_dir.mkdir(parents=True)
    (custom_dir / "guide.md").write_text("hello", encoding="utf-8")
    ctx = ToolExecutionContext(config_manager=_ConfigManager(suite_root))

    result = read_suite_doc(filename="guide.md", _tool_context=ctx)

    assert result["content"] == "hello"
    assert result["path"] == str((custom_dir / "guide.md").resolve())
