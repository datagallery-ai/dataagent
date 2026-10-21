"""Windows-only host path acceptance; non-Windows behavior must stay unchanged."""

import sys
from pathlib import Path

import pytest

from dataagent.core.backends.filesystem import FilesystemBackend
from dataagent.core.backends.local_shell import LocalShellBackend, _maybe_append_cmd_hint
from dataagent.core.backends.utils import _paths_overlap, validate_path


def test_validate_path_rejects_drive_letters_off_windows(monkeypatch):
    monkeypatch.setattr("dataagent.core.backends.utils.sys.platform", "linux")
    with pytest.raises(ValueError, match="Windows absolute paths are not supported"):
        validate_path(r"C:\Users\admin\file.txt")
    assert validate_path("/workspace/file.txt") == "/workspace/file.txt"


@pytest.mark.skipif(sys.platform != "win32", reason="host drive paths only on Windows")
def test_validate_path_accepts_drive_and_unc_on_windows():
    assert validate_path(r"C:\Users\admin\file.txt") == "C:/Users/admin/file.txt"
    assert validate_path("C:/Users/admin/file.txt") == "C:/Users/admin/file.txt"
    assert validate_path(r"\\server\share\dir\a.txt") == "//server/share/dir/a.txt"
    with pytest.raises(ValueError, match="Path traversal"):
        validate_path(r"C:\Users\admin\..\secret.txt")
    with pytest.raises(ValueError, match="Windows absolute paths are not supported"):
        validate_path("C:relative.txt")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path overlap only on win32")
def test_paths_overlap_windows_drive_letters():
    assert _paths_overlap("C:/Users/admin/a", "C:/Users/admin")
    assert _paths_overlap("C:/Users/admin", "C:/Users/admin/a")
    assert not _paths_overlap("C:/Users/admin", "C:/Users/other")
    # Mixed with POSIX shapes still uses Windows compare when either side is drive-shaped.
    assert _paths_overlap("C:/data", "C:/data/file.txt")


def test_paths_overlap_posix_unchanged_off_windows(monkeypatch):
    monkeypatch.setattr("dataagent.core.backends.utils.sys.platform", "linux")
    assert _paths_overlap("/secret", "/secret/child")
    assert not _paths_overlap("/secret", "/secrets")


@pytest.mark.skipif(sys.platform != "win32", reason="bare POSIX reject only on Windows host mode")
def test_resolve_rejects_bare_posix_when_not_virtual(tmp_path):
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=False)
    with pytest.raises(ValueError, match="POSIX-style absolute paths are not supported"):
        backend._resolve_path("/workspace/inspect.py")
    target = tmp_path / "ok.txt"
    target.write_text("x", encoding="utf-8")
    assert backend._resolve_path(str(target)) == Path(str(target))


@pytest.mark.skipif(sys.platform != "win32", reason="host write roundtrip on Windows")
def test_non_virtual_write_read_with_drive_path(tmp_path):
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=False)
    path = validate_path(str(tmp_path / "out.txt"))
    written = backend.write(path, "hello-windows")
    assert written.error is None
    read = backend.read(path)
    assert read.error is None
    assert "hello-windows" in (read.file_data or {}).get("content", "")


def test_virtual_mode_still_accepts_posix_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", sys.platform)  # no-op; documents intent
    backend = FilesystemBackend(root_dir=tmp_path, virtual_mode=True)
    resolved = backend._resolve_path("/workspace/file.txt")
    assert resolved == (tmp_path / "workspace" / "file.txt").resolve()


def test_cmd_hint_only_on_win32_failures(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    text = "ls is not recognized as an internal or external command"
    assert _maybe_append_cmd_hint(text, 1) == text
    monkeypatch.setattr(sys, "platform", "win32")
    hinted = _maybe_append_cmd_hint(text, 1)
    assert "cmd.exe" in hinted
    assert _maybe_append_cmd_hint("ok", 0) == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe integration")
def test_execute_appends_cmd_hint_for_unknown_command(tmp_path):
    backend = LocalShellBackend(root_dir=tmp_path, virtual_mode=False, inherit_env=True)
    result = backend.execute("this_command_should_not_exist_dataagent_xyz")
    assert result.exit_code != 0
    assert "cmd.exe" in result.output
