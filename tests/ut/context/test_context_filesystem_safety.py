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

from dataagent.core.context.context_ir import FileNode
from dataagent.core.context.utils_context_filesystem import extract_file_paths_from_query, load_file


def test_extract_file_paths_from_query_ignores_paths_outside_allowed_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "note.txt"
    outside = tmp_path / "secret.txt"
    inside.write_text("inside", encoding="utf-8")
    outside.write_text("secret", encoding="utf-8")

    paths = extract_file_paths_from_query(
        query=f"read {inside} and {outside}",
        allowed_roots=[workspace],
    )

    assert paths == {"Table": [], "File": [str(inside.resolve())]}


def test_load_file_rejects_allowed_root_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    try:
        load_file(filepath=str(outside), allowed_roots=[workspace])
    except ValueError as exc:
        assert "outside allowed roots" in str(exc)
    else:
        raise AssertionError("load_file should reject paths outside allowed roots")


def test_file_node_does_not_preview_file_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    node = FileNode(
        label="file00000",
        description="",
        user_id="u",
        session_id="s",
        run_id=1,
        path=str(outside),
        source="user",
        workspace_root=str(workspace),
    )

    assert "Cannot read" in node.get_full_data()
