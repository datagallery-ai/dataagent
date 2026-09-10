"""Filesystem and shell must agree on user-selected host workspace paths."""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

from dataagent.core.deepagents.config.middleware import NativeMiddlewareConfigCompiler
from dataagent.core.deepagents.config.workspace import WorkspaceConfigCompiler
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend


def test_filesystem_script_and_shell_share_user_workspace(tmp_path: Path) -> None:
    root = tmp_path / "user workspace"
    compiled = WorkspaceConfigCompiler({"WORKSPACE": {"path": str(root)}}).compile()
    backend = compiled.backend
    assert isinstance(backend, FilesystemBackend)
    assert backend.virtual_mode is False
    middleware = NativeMiddlewareConfigCompiler(
        models={}, primary_model_name="chat", workspace_root=compiled.workspace_root, shell_enabled=True,
    ).compile()
    shell = next(item for item in middleware if type(item).__name__ == "ShellToolMiddleware")
    assert shell._workspace_root == root

    script = (
        "import csv\nwith open('advertising_data.csv', 'w', newline='') as f:\n"
        "    w = csv.writer(f)\n    w.writerow(['impressions', 'clicks'])\n"
        "    w.writerows((1000 + i, 10 + i) for i in range(100))\n"
    )
    assert backend.write("generate.py", script).error is None
    result = subprocess.run(
        [sys.executable, str(root / "generate.py")], cwd=shell._workspace_root,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    output = root / "advertising_data.csv"
    with output.open() as handle:
        assert len(list(csv.DictReader(handle))) == 100
    assert backend.read(str(output)).error is None
    assert backend.edit(str(output), "impressions,clicks", "views,clicks").error is None
    assert output.read_text().startswith("views,clicks")
    assert not (root / str(root).lstrip("/")).exists()
    assert "real host paths" in compiled.system_prompt
    assert "mounted at `/`" not in compiled.system_prompt


def test_absolute_paths_are_not_rebased_and_session_fallback_remains(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "home"))
    compiled = WorkspaceConfigCompiler({}, user_id="alice", session_id="session-1").compile()
    assert compiled.workspace_root == tmp_path / "home" / "alice" / "session-1"
    outside = tmp_path / "explicit-output.txt"
    assert compiled.backend.write(str(outside), "explicit host path").error is None
    assert outside.read_text() == "explicit host path"


def test_state_and_explicit_virtual_backends_keep_their_path_semantics(tmp_path: Path) -> None:
    state = WorkspaceConfigCompiler({"WORKSPACE": {"backend": "state"}}).compile()
    assert isinstance(state.backend, StateBackend)
    assert not state.shell_enabled
    virtual = WorkspaceConfigCompiler({}, backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True)).compile()
    assert "mounted at `/`" in virtual.system_prompt
    assert virtual.backend.write("/virtual.txt", "virtual").error is None
    assert (tmp_path / "virtual.txt").read_text() == "virtual"


def test_additional_readonly_mount_keeps_its_route_and_permission(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    compiled = WorkspaceConfigCompiler({"WORKSPACE": {
        "path": str(tmp_path / "workspace"), "allow_path": [str(reference)],
    }}).compile()
    assert isinstance(compiled.backend, CompositeBackend)
    assert compiled.backend.default.virtual_mode is False
    assert compiled.backend.routes[f"{reference}/"].virtual_mode is True
    assert compiled.permissions[0].mode == "deny"
    assert compiled.permissions[0].operations == ["write"]
    assert f"{reference}/**" in compiled.permissions[0].paths
