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
import asyncio
from pathlib import Path

import pytest

from dataagent.actions.tools.local_tool import tools
from dataagent.actions.tools.local_tool.sandbox import (
    NoopSandbox,
    WorkspaceAccessError,
    get_current_sandbox,
    reset_current_sandbox,
    set_current_sandbox,
)


def test_read_file_requires_bound_sandbox(tmp_path: Path):
    target = tmp_path / "demo.txt"
    target.write_text("hello", encoding="utf-8")

    with pytest.raises(RuntimeError, match="sandbox"):
        tools.read_file(str(target), purpose="read without sandbox")


def test_read_file_uses_bound_sandbox(tmp_path: Path):
    workspace = tmp_path.resolve()
    target = workspace / "demo.txt"
    target.write_text("hello", encoding="utf-8")
    token = set_current_sandbox(NoopSandbox(workspace_root=workspace))

    try:
        result = tools.read_file(str(target), purpose="read with sandbox")
    finally:
        reset_current_sandbox(token)

    assert result["original_msg"] == "1\thello"
    assert result["data"]["path"] == str(target.resolve())


def test_concurrent_tool_context_isolates_workspace_and_skill_aliases(tmp_path: Path):
    workspace_a = (tmp_path / "workspace-a").resolve()
    workspace_b = (tmp_path / "workspace-b").resolve()
    skill_root_a = (tmp_path / "skill-a").resolve()
    skill_root_b = (tmp_path / "skill-b").resolve()
    workspace_a.mkdir(parents=True, exist_ok=True)
    workspace_b.mkdir(parents=True, exist_ok=True)
    skill_root_a.mkdir(parents=True, exist_ok=True)
    skill_root_b.mkdir(parents=True, exist_ok=True)
    (workspace_a / "a.txt").write_text("from-a", encoding="utf-8")
    (workspace_b / "b.txt").write_text("from-b", encoding="utf-8")

    async def _worker(workspace: Path, skill_root: Path, filename: str) -> str:
        sandbox = NoopSandbox(
            workspace_root=workspace,
            skill_aliases={"pdf": skill_root},
        )
        token = set_current_sandbox(sandbox)
        try:
            current = get_current_sandbox()
            assert current.workspace_root == workspace
            assert current.skill_aliases == {"pdf": skill_root}
            target = workspace / filename
            result = tools.read_file(str(target), purpose="concurrent read")
            return result["original_msg"]
        finally:
            reset_current_sandbox(token)

    async def _run_workers():
        return await asyncio.gather(
            _worker(workspace_a, skill_root_a, "a.txt"),
            _worker(workspace_b, skill_root_b, "b.txt"),
        )

    content_a, content_b = asyncio.run(_run_workers())
    assert content_a == "1\tfrom-a"
    assert content_b == "1\tfrom-b"


def test_concurrent_tool_context_isolates_allow_read_roots(tmp_path: Path):
    workspace_a = (tmp_path / "workspace-a").resolve()
    workspace_b = (tmp_path / "workspace-b").resolve()
    allow_a = (tmp_path / "allow-a").resolve()
    allow_b = (tmp_path / "allow-b").resolve()
    for d in (workspace_a, workspace_b, allow_a, allow_b):
        d.mkdir(parents=True, exist_ok=True)
    file_a = allow_a / "data.txt"
    file_b = allow_b / "data.txt"
    file_a.write_text("allow-a", encoding="utf-8")
    file_b.write_text("allow-b", encoding="utf-8")

    async def _worker(allow_root: Path, target_file: Path) -> str:
        sandbox = NoopSandbox(
            workspace_root=allow_root,
            allow_read_roots=[allow_root],
        )
        token = set_current_sandbox(sandbox)
        try:
            other_allow = allow_b if allow_root == allow_a else allow_a
            other_target = other_allow / "data.txt"
            with pytest.raises(WorkspaceAccessError):
                tools.read_file(str(other_target), purpose="cross root should fail")
            result = tools.read_file(str(target_file), purpose="own root should pass")
            return result["original_msg"]
        finally:
            reset_current_sandbox(token)

    async def _run_workers():
        return await asyncio.gather(
            _worker(allow_a, file_a),
            _worker(allow_b, file_b),
        )

    content_a, content_b = asyncio.run(_run_workers())
    assert content_a == "1\tallow-a"
    assert content_b == "1\tallow-b"
