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
import inspect
from pathlib import Path

import pytest

from dataagent.actions.tools.local_tool.sandbox import NoopSandbox, create_sandbox


def test_sandbox_init_rejects_relative_allow_read_paths():
    with pytest.raises(ValueError, match="absolute"):
        NoopSandbox(workspace_root=None, allow_read_roots=["./output"])


def test_sandbox_init_accepts_absolute_allow_read_paths(tmp_path: Path):
    output_dir = (tmp_path / "output").resolve()
    skills_dir = (tmp_path / "skills").resolve()

    sb = NoopSandbox(workspace_root=None, allow_read_roots=[output_dir, skills_dir])

    assert sb.allow_read_roots == [output_dir, skills_dir]


def test_sandbox_constructor_signature():
    params = inspect.signature(NoopSandbox.__init__).parameters

    assert list(params) == ["self", "workspace_root", "skill_aliases", "allow_read_roots"]


def test_sandbox_instances_should_isolate_roots_and_aliases(tmp_path: Path):
    workspace_a = (tmp_path / "workspace-a").resolve()
    workspace_b = (tmp_path / "workspace-b").resolve()
    shared_read_a = (tmp_path / "read-a").resolve()
    shared_read_b = (tmp_path / "read-b").resolve()
    skill_root_a = (tmp_path / "skill-a").resolve()
    skill_root_b = (tmp_path / "skill-b").resolve()

    sandbox_a = NoopSandbox(
        workspace_root=workspace_a,
        skill_aliases={"pdf": skill_root_a},
        allow_read_roots=[shared_read_a],
    )
    sandbox_b = NoopSandbox(
        workspace_root=workspace_b,
        skill_aliases={"pdf": skill_root_b},
        allow_read_roots=[shared_read_b],
    )

    assert sandbox_a.workspace_root == workspace_a
    assert sandbox_b.workspace_root == workspace_b
    assert sandbox_a.allow_read_roots == [shared_read_a]
    assert sandbox_b.allow_read_roots == [shared_read_b]
    assert sandbox_a.skill_aliases == {"pdf": skill_root_a}
    assert sandbox_b.skill_aliases == {"pdf": skill_root_b}
    assert sandbox_a.resolve_requested_path("a.txt", sandbox_a.workspace_root) == workspace_a / "a.txt"
    assert sandbox_b.resolve_requested_path("b.txt", sandbox_b.workspace_root) == workspace_b / "b.txt"
    assert sandbox_a.resolve_prompt_path_alias("skill/pdf/SKILL.md") == skill_root_a / "SKILL.md"
    assert sandbox_b.resolve_prompt_path_alias("skill/pdf/SKILL.md") == skill_root_b / "SKILL.md"
