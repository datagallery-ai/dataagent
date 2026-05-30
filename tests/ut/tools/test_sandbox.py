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
"""Tests for dataagent.actions.tools.local_tool.sandbox."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from dataagent.actions.tools.local_tool.sandbox import (
    BubblewrapSandbox,
    NoopSandbox,
    SandboxPolicy,
    build_workspace_mount_lists,
    create_sandbox,
    is_bwrap_sandbox_usable,
    os_config_bind_paths,
    reset_current_sandbox,
    runtime_python_bind_paths,
    set_current_sandbox,
)


def _bwrap_functional() -> bool:
    """Check whether bwrap can actually create a sandbox."""
    if not shutil.which("bwrap"):
        return False
    import subprocess

    try:
        result = subprocess.run(
            ["bwrap", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


requires_bwrap = pytest.mark.skipif(not _bwrap_functional(), reason="bwrap not installed or lacks privileges")


class TestSandboxPolicy:
    def test_defaults(self):
        policy = SandboxPolicy()
        assert "/usr" in policy.readonly_binds
        assert "/lib" in policy.readonly_binds
        assert policy.writable_binds == []
        assert "/tmp" in policy.tmpfs_paths
        assert policy.unshare_net is False
        assert policy.die_with_parent is True
        assert policy.proc_path == "/proc"
        assert policy.dev_path == "/dev"

    def test_custom_values(self):
        policy = SandboxPolicy(
            readonly_binds=["/opt"],
            writable_binds=["/workspace"],
            tmpfs_paths=["/run"],
            unshare_net=False,
            die_with_parent=False,
        )
        assert policy.readonly_binds == ["/opt"]
        assert policy.writable_binds == ["/workspace"]
        assert policy.tmpfs_paths == ["/run"]
        assert policy.unshare_net is False
        assert policy.die_with_parent is False

    def test_frozen(self):
        policy = SandboxPolicy()
        with pytest.raises(AttributeError):
            policy.unshare_net = True  # type: ignore[misc]


class TestBubblewrapSandbox:
    def test_wrap_command_basic(self, tmp_path: Path):
        ro = str(tmp_path / "ro")
        rw = str(tmp_path / "rw")
        Path(ro).mkdir()
        Path(rw).mkdir()

        policy = SandboxPolicy(
            readonly_binds=[ro],
            writable_binds=[rw],
            tmpfs_paths=["/tmp"],
            unshare_net=True,
            die_with_parent=True,
        )
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["python", "script.py"])

        assert result[0] == "bwrap"
        assert "--ro-bind" in result
        ro_idx = result.index("--ro-bind")
        assert result[ro_idx + 1] == ro
        assert "--bind" in result
        bind_idx = result.index("--bind")
        assert result[bind_idx + 1] == rw
        assert "--tmpfs" in result
        assert "--unshare-net" in result
        assert "--die-with-parent" in result
        assert result[-2:] == ["python", "script.py"]

    def test_wrap_command_skips_nonexistent_paths(self):
        policy = SandboxPolicy(
            readonly_binds=["/nonexistent_path_abc123"],
            writable_binds=["/nonexistent_path_xyz789"],
        )
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["echo", "hi"])
        assert "/nonexistent_path_abc123" not in result
        assert "/nonexistent_path_xyz789" not in result

    def test_wrap_command_with_cwd(self):
        policy = SandboxPolicy(readonly_binds=[], writable_binds=[])
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["ls"], cwd="/some/dir")
        chdir_idx = result.index("--chdir")
        assert result[chdir_idx + 1] == "/some/dir"

    def test_wrap_command_no_cwd(self):
        policy = SandboxPolicy(readonly_binds=[], writable_binds=[])
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["ls"])
        assert "--chdir" not in result

    def test_wrap_command_namespace_flags_disabled(self, tmp_path: Path):
        policy = SandboxPolicy(
            readonly_binds=[],
            writable_binds=[],
            unshare_net=False,
            die_with_parent=False,
        )
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["echo"])
        assert "--unshare-net" not in result
        assert "--die-with-parent" not in result

    def test_wrap_command_includes_proc_and_dev_by_default(self):
        """默认策略下，bwrap 必须挂入 /proc 与 /dev，否则 bash <()、/dev/null 等会失败。"""
        sb = BubblewrapSandbox(SandboxPolicy(readonly_binds=[], writable_binds=[]))
        result = sb.wrap_command(["echo"])

        proc_idx = result.index("--proc")
        assert result[proc_idx + 1] == "/proc"
        dev_idx = result.index("--dev")
        assert result[dev_idx + 1] == "/dev"

    def test_wrap_command_proc_dev_can_be_disabled(self):
        """显式置 None 时不发出 --proc / --dev，便于特殊场景关闭命名空间。"""
        policy = SandboxPolicy(readonly_binds=[], writable_binds=[], proc_path=None, dev_path=None)
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["echo"])
        assert "--proc" not in result
        assert "--dev" not in result

    def test_wrap_command_proc_dev_custom_targets(self):
        """支持自定义挂载点（极少用，但保留对称性）。"""
        policy = SandboxPolicy(readonly_binds=[], writable_binds=[], proc_path="/custom_proc", dev_path="/custom_dev")
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["echo"])
        assert result[result.index("--proc") : result.index("--proc") + 2] == ["--proc", "/custom_proc"]
        assert result[result.index("--dev") : result.index("--dev") + 2] == ["--dev", "/custom_dev"]

    def test_is_available_with_bwrap(self):
        sb = BubblewrapSandbox(SandboxPolicy())
        with patch("dataagent.actions.tools.local_tool.sandbox.is_bwrap_sandbox_usable", return_value=True):
            assert sb.is_available() is True

    def test_is_available_without_bwrap(self):
        sb = BubblewrapSandbox(SandboxPolicy())
        with patch("dataagent.actions.tools.local_tool.sandbox.is_bwrap_sandbox_usable", return_value=False):
            assert sb.is_available() is False


class TestNoopSandbox:
    def test_wrap_command_passthrough(self):
        sb = NoopSandbox()
        cmd = ["python", "script.py"]
        assert sb.wrap_command(cmd) is cmd

    def test_is_available(self):
        assert NoopSandbox().is_available() is True


class TestCreateSandbox:
    def test_disabled_returns_noop(self):
        sb = create_sandbox(enabled=False)
        assert isinstance(sb, NoopSandbox)

    def test_enabled_with_bwrap(self):
        with patch("dataagent.actions.tools.local_tool.sandbox.is_bwrap_sandbox_usable", return_value=True):
            sb = create_sandbox(enabled=True)
            assert isinstance(sb, BubblewrapSandbox)

    def test_enabled_without_bwrap_falls_back(self):
        with patch("dataagent.actions.tools.local_tool.sandbox.is_bwrap_sandbox_usable", return_value=False):
            sb = create_sandbox(enabled=True)
            assert isinstance(sb, NoopSandbox)
            assert os.environ["DATAAGENT_SANDBOX_ENABLED"] == "false"

    def test_enabled_bwrap_probe_failure_sets_env_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("DATAAGENT_SANDBOX_ENABLED", raising=False)
        monkeypatch.delenv("DATAAGENT_SANDBOX_ENABLED", raising=False)
        with patch("dataagent.actions.tools.local_tool.sandbox.is_bwrap_sandbox_usable", return_value=False):
            sb = create_sandbox(enabled=True)
        assert isinstance(sb, NoopSandbox)
        assert os.environ["DATAAGENT_SANDBOX_ENABLED"] == "false"

    def test_custom_policy(self):
        policy = SandboxPolicy(readonly_binds=["/opt"], writable_binds=["/data"])
        with patch.object(BubblewrapSandbox, "is_available", return_value=True) as available_mock:
            sb = create_sandbox(enabled=True, policy=policy)
            assert isinstance(sb, BubblewrapSandbox)
            assert sb._policy is policy  # noqa: SLF001
        available_mock.assert_called_once_with()

    def test_create_sandbox_uses_bubblewrap_is_available(self):
        with patch.object(BubblewrapSandbox, "is_available", return_value=False) as available_mock:
            sb = create_sandbox(enabled=True)
        assert isinstance(sb, NoopSandbox)
        available_mock.assert_called_once_with()


class TestBwrapSandboxUsable:
    def setup_method(self):
        is_bwrap_sandbox_usable.cache_clear()

    def test_false_when_bwrap_missing(self):
        with patch("shutil.which", return_value=None):
            assert is_bwrap_sandbox_usable() is False

    def test_false_when_probe_fails(self):
        completed = subprocess.CompletedProcess(args=["bwrap"], returncode=1, stderr=b"no namespace")
        with (
            patch("shutil.which", return_value="/usr/bin/bwrap"),
            patch("subprocess.run", return_value=completed),
        ):
            assert is_bwrap_sandbox_usable() is False

    def test_true_when_probe_succeeds(self):
        completed = subprocess.CompletedProcess(args=["bwrap"], returncode=0)
        with (
            patch("shutil.which", return_value="/usr/bin/bwrap"),
            patch("subprocess.run", return_value=completed),
        ):
            assert is_bwrap_sandbox_usable() is True

    def test_probe_result_is_cached(self):
        completed = subprocess.CompletedProcess(args=["bwrap"], returncode=0)
        with (
            patch("shutil.which", return_value="/usr/bin/bwrap"),
            patch("subprocess.run", return_value=completed) as run_mock,
        ):
            assert is_bwrap_sandbox_usable() is True
            assert is_bwrap_sandbox_usable() is True
        assert run_mock.call_count == 1


class TestBuildWorkspaceMountLists:
    """``build_workspace_mount_lists`` 是 DataAgent 的默认挂载策略生成器；纯函数。"""

    def test_workspace_appears_in_writable(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        readonly_binds, writable_binds = build_workspace_mount_lists(resolved_workspace=ws)
        assert str(ws) in writable_binds

    def test_l0_system_paths_always_in_readonly(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        readonly_binds, _ = build_workspace_mount_lists(resolved_workspace=ws)
        for required in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"):
            assert required in readonly_binds, f"{required} must be in readonly_binds"

    def test_skips_nonexistent_optional_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # 空 HOME 下不存在的可选工具链不应被挂载
        monkeypatch.setenv("HOME", str(tmp_path))
        ws = tmp_path / "ws"
        ws.mkdir()
        readonly_binds, writable_binds = build_workspace_mount_lists(resolved_workspace=ws)

        for missing in (".cargo/bin", ".nvm", ".pyenv", ".rustup", ".bun", ".deno", ".pipx", ".poetry"):
            assert not any(missing in p for p in readonly_binds), f"{missing} should be skipped"

        assert "/etc" in readonly_binds
        assert str(ws) in writable_binds
        assert not any(".cache" in p for p in writable_binds)

    def test_os_config_bind_paths_follows_resolv_symlink(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """symlink 目标在 L0 外时，应 bind 解析后的目录（替代硬编码 /mnt/wsl）。"""
        import dataagent.actions.tools.local_tool.sandbox as sandbox_module

        mnt_wsl = tmp_path / "mnt_wsl"
        mnt_wsl.mkdir()
        (mnt_wsl / "resolv.conf").write_text("nameserver 127.0.0.1\n", encoding="utf-8")
        etc_resolv = tmp_path / "etc" / "resolv.conf"
        etc_resolv.parent.mkdir(parents=True)
        etc_resolv.symlink_to(mnt_wsl / "resolv.conf")

        monkeypatch.setattr(sandbox_module, "_OS_CONFIG_PATHS", (str(etc_resolv),))
        binds = os_config_bind_paths([str(tmp_path / "etc")])
        assert str(mnt_wsl) in binds

    def test_includes_existing_user_toolchain_and_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        fake_home = tmp_path / "home"
        (fake_home / ".local/bin").mkdir(parents=True)
        (fake_home / ".cache").mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        ws = tmp_path / "ws"
        ws.mkdir()

        readonly_binds, writable_binds = build_workspace_mount_lists(resolved_workspace=ws)
        assert str(fake_home / ".local/bin") in readonly_binds
        assert str(fake_home / ".cache") in writable_binds

    def test_allow_read_roots_appended_to_readonly(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        extra = tmp_path / "extra"
        extra.mkdir()
        readonly_binds, _ = build_workspace_mount_lists(
            resolved_workspace=ws,
            allow_read_roots=[extra],
        )
        assert str(extra) in readonly_binds

    def test_skill_aliases_dedup_and_append(self, tmp_path: Path):
        ws = tmp_path / "ws"
        ws.mkdir()
        skill_a = tmp_path / "skill_a"
        skill_a.mkdir()
        # 两个 alias 指向同一目录，应只出现一次
        readonly_binds, _ = build_workspace_mount_lists(
            resolved_workspace=ws,
            skill_aliases={"a1": skill_a, "a2": skill_a},
        )
        assert readonly_binds.count(str(skill_a)) == 1

    def test_default_args_optional(self, tmp_path: Path):
        """allow_read_roots 与 skill_aliases 都可缺省。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        readonly_binds, writable_binds = build_workspace_mount_lists(resolved_workspace=ws)
        assert str(ws) in writable_binds
        assert "/usr" in readonly_binds


class TestRuntimePythonBindPaths:
    def test_skips_prefix_under_l0(self, monkeypatch: pytest.MonkeyPatch):
        import dataagent

        monkeypatch.setattr(sys, "prefix", "/usr")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
        monkeypatch.setattr(dataagent, "__file__", "/usr/lib/python3/dist-packages/dataagent/__init__.py")
        assert runtime_python_bind_paths() == []

    def test_includes_custom_prefix_outside_l0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import dataagent

        prefix = tmp_path / "opt" / "python-3.11"
        pkg = prefix / "lib" / "python3.11" / "site-packages" / "dataagent"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").touch()

        monkeypatch.setattr(sys, "prefix", str(prefix))
        monkeypatch.setattr(sys, "base_prefix", str(prefix))
        monkeypatch.setattr(sys, "executable", str(prefix / "bin" / "python3"))
        monkeypatch.setattr(dataagent, "__file__", str(pkg / "__init__.py"))

        binds = runtime_python_bind_paths()
        assert binds == [str(prefix.resolve())]

    def test_includes_editable_dataagent_package_outside_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import dataagent

        prefix = tmp_path / "venv"
        prefix.mkdir()
        pkg = tmp_path / "repo" / "dataagent"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").touch()

        monkeypatch.setattr(sys, "prefix", str(prefix))
        monkeypatch.setattr(sys, "base_prefix", str(prefix))
        monkeypatch.setattr(sys, "executable", str(prefix / "bin" / "python3"))
        monkeypatch.setattr(dataagent, "__file__", str(pkg / "__init__.py"))

        binds = runtime_python_bind_paths()
        assert str(prefix.resolve()) in binds
        assert str(pkg.resolve()) in binds

    def test_includes_base_prefix_when_different_from_venv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """uv 风格 venv：sys.executable 指向 base_prefix 下的真实解释器。"""
        venv = tmp_path / "project" / ".venv"
        real_python = tmp_path / "share" / "uv" / "python"
        (real_python / "bin").mkdir(parents=True)
        real_exe = real_python / "bin" / "python3.11"
        real_exe.write_bytes(b"")
        venv_bin = venv / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").symlink_to(real_exe)

        monkeypatch.setattr(sys, "prefix", str(venv))
        monkeypatch.setattr(sys, "base_prefix", str(real_python))
        monkeypatch.setattr(sys, "executable", str(venv_bin / "python3"))

        binds = runtime_python_bind_paths()
        assert str(venv.resolve()) in binds
        assert str(real_python.resolve()) in binds

    def test_includes_venv_prefix_for_local_dev(self, monkeypatch: pytest.MonkeyPatch):
        prefix = Path(sys.prefix).resolve()
        if _covered_by_sandbox_l0(prefix):
            pytest.skip("current interpreter prefix is under L0")

        binds = runtime_python_bind_paths()
        assert str(prefix) in binds

    def test_build_workspace_mount_lists_includes_runtime_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        prefix = tmp_path / "custom-prefix"
        (prefix / "bin").mkdir(parents=True)
        monkeypatch.setattr(sys, "prefix", str(prefix))
        monkeypatch.setattr(sys, "base_prefix", str(prefix))
        monkeypatch.setattr(sys, "executable", str(prefix / "bin" / "python3"))

        ws = tmp_path / "ws"
        ws.mkdir()
        readonly_binds, _ = build_workspace_mount_lists(resolved_workspace=ws)
        assert str(prefix.resolve()) in readonly_binds


def _covered_by_sandbox_l0(path: Path) -> bool:
    l0 = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc")
    for root in l0:
        try:
            path.resolve().relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


class TestSandboxPathAudit:
    def test_sandbox_with_workspace(self, tmp_path: Path):
        sb = NoopSandbox(workspace_root=tmp_path)
        assert sb.workspace_root == tmp_path

    def test_sandbox_without_workspace(self):
        sb = NoopSandbox()
        assert sb.workspace_root is None

    def test_sandbox_default_no_workspace(self):
        sb = create_sandbox(enabled=False)
        assert sb.workspace_root is None


class TestBubblewrapMountOrdering:
    """Verify that tmpfs is mounted before all binds so child paths override."""

    def test_tmpfs_before_writable_binds(self, tmp_path: Path):
        rw = tmp_path / "workspace"
        rw.mkdir()
        policy = SandboxPolicy(
            readonly_binds=[],
            writable_binds=[str(rw)],
            tmpfs_paths=[str(tmp_path)],
        )
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["echo"])
        tmpfs_idx = result.index("--tmpfs")
        bind_idx = result.index("--bind")
        assert tmpfs_idx < bind_idx, "tmpfs must come before writable bind"

    def test_tmpfs_before_readonly_binds(self, tmp_path: Path):
        ro = tmp_path / "data"
        ro.mkdir()
        policy = SandboxPolicy(
            readonly_binds=[str(ro)],
            writable_binds=[],
            tmpfs_paths=[str(tmp_path)],
        )
        sb = BubblewrapSandbox(policy)
        result = sb.wrap_command(["echo"])
        tmpfs_idx = result.index("--tmpfs")
        ro_idx = result.index("--ro-bind")
        assert tmpfs_idx < ro_idx, "tmpfs must come before readonly bind"


@requires_bwrap
class TestBubblewrapIntegration:
    """Integration tests that actually invoke bwrap. Skipped if bwrap is not installed."""

    def test_filesystem_isolation(self, tmp_path: Path):
        """Sandbox should not see /home."""
        import subprocess

        workspace = tmp_path / "ws"
        workspace.mkdir()
        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin"],
            writable_binds=[str(workspace)],
        )
        sb = BubblewrapSandbox(policy)
        cmd = sb.wrap_command(["ls", "/home"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "No such file" in result.stderr

    def test_readonly_enforcement(self):
        """Cannot write to ro-bind paths."""
        import subprocess

        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin"],
            writable_binds=[],
        )
        sb = BubblewrapSandbox(policy)
        cmd = sb.wrap_command(["touch", "/usr/should_fail"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "Read-only file system" in result.stderr

    def test_writable_bind_works(self, tmp_path: Path):
        """Can write to writable_binds paths."""
        import subprocess

        workspace = tmp_path / "ws"
        workspace.mkdir()
        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin"],
            writable_binds=[str(workspace)],
        )
        sb = BubblewrapSandbox(policy)
        target = workspace / "test_file"
        cmd = sb.wrap_command(["touch", str(target)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert target.exists()

    def test_network_isolation(self):
        """Sandbox with unshare_net should not reach the network."""
        import subprocess

        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin"],
            writable_binds=[],
            unshare_net=True,
        )
        sb = BubblewrapSandbox(policy)
        cmd = sb.wrap_command(["/bin/sh", "-c", "cat /sys/class/net/eth0/address 2>&1 || echo no-eth0"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert "no-eth0" in result.stdout or "No such file" in result.stderr or result.returncode != 0

    def test_writable_bind_under_tmpfs(self, tmp_path: Path):
        """writable_binds under a tmpfs parent should still be writable (mount ordering)."""
        import subprocess

        workspace = tmp_path / "ws"
        workspace.mkdir()
        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin"],
            writable_binds=[str(workspace)],
            tmpfs_paths=[str(tmp_path)],
        )
        sb = BubblewrapSandbox(policy)
        target = workspace / "nested_ok"
        cmd = sb.wrap_command(["touch", str(target)])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f"Write failed: {result.stderr}"
        assert target.exists()


@requires_bwrap
class TestRunSubprocessAsyncIntegration:
    """Test _run_subprocess_async with sandbox via contextvars."""

    @pytest.mark.asyncio
    async def test_subprocess_with_sandbox(self, tmp_path: Path):
        from dataagent.actions.tools.local_tool.tools import _run_subprocess_async

        workspace = tmp_path / "ws"
        workspace.mkdir()
        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin"],
            writable_binds=[str(workspace)],
        )
        sandbox = BubblewrapSandbox(policy, workspace_root=workspace)
        token = set_current_sandbox(sandbox)
        try:
            result = await _run_subprocess_async(
                ["/bin/sh", "-c", "echo hello"],
                timeout=10,
                cwd=str(workspace),
            )
            assert result["returncode"] == 0
            assert "hello" in result["stdout"]
        finally:
            reset_current_sandbox(token)

    @pytest.mark.asyncio
    async def test_subprocess_sandbox_blocks_home(self, tmp_path: Path):
        from dataagent.actions.tools.local_tool.tools import _run_subprocess_async

        workspace = tmp_path / "ws"
        workspace.mkdir()
        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin"],
            writable_binds=[str(workspace)],
        )
        sandbox = BubblewrapSandbox(policy, workspace_root=workspace)
        token = set_current_sandbox(sandbox)
        try:
            result = await _run_subprocess_async(
                ["/bin/sh", "-c", "ls /home"],
                timeout=10,
                cwd=str(workspace),
            )
            assert result["returncode"] != 0
            assert "No such file" in result["stderr"]
        finally:
            reset_current_sandbox(token)

    @pytest.mark.asyncio
    async def test_subprocess_without_sandbox_passthrough(self, tmp_path: Path):
        """When NoopSandbox is used, commands run without bwrap."""
        from dataagent.actions.tools.local_tool.tools import _run_subprocess_async

        sandbox = NoopSandbox(workspace_root=tmp_path)
        token = set_current_sandbox(sandbox)
        try:
            result = await _run_subprocess_async(
                ["/bin/sh", "-c", "ls /home"],
                timeout=10,
                cwd=str(tmp_path),
            )
            assert result["returncode"] == 0
        finally:
            reset_current_sandbox(token)


@requires_bwrap
class TestBubblewrapReadonlyRoots:
    """Verify that allow_read_roots and skill roots are accessible as ro-bind inside the sandbox."""

    def test_allow_read_roots_visible_and_readonly(self, tmp_path: Path):
        """Paths from allow_read_roots should be readable but not writable in the sandbox."""
        import subprocess

        workspace = tmp_path / "ws"
        workspace.mkdir()
        read_root = tmp_path / "shared_data"
        read_root.mkdir()
        (read_root / "info.txt").write_text("hello from read root")

        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin", str(read_root)],
            writable_binds=[str(workspace)],
        )
        sb = BubblewrapSandbox(policy)

        # readable
        cmd = sb.wrap_command(["cat", str(read_root / "info.txt")])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert "hello from read root" in result.stdout

        # not writable
        cmd = sb.wrap_command(["touch", str(read_root / "should_fail")])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "Read-only file system" in result.stderr

    def test_skill_roots_visible_and_readonly(self, tmp_path: Path):
        """Skill root directories should be readable but not writable in the sandbox."""
        import subprocess

        workspace = tmp_path / "ws"
        workspace.mkdir()
        skill_root = tmp_path / "skills" / "data_analysis"
        skill_root.mkdir(parents=True)
        (skill_root / "run.py").write_text("print('skill script')")

        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin", str(skill_root)],
            writable_binds=[str(workspace)],
        )
        sb = BubblewrapSandbox(policy)

        # readable
        cmd = sb.wrap_command(["cat", str(skill_root / "run.py")])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert "skill script" in result.stdout

        # not writable
        cmd = sb.wrap_command(["touch", str(skill_root / "should_fail")])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "Read-only file system" in result.stderr

    def test_multiple_readonly_roots_all_visible(self, tmp_path: Path):
        """Multiple allow_read_roots and skill roots should all be visible."""
        import subprocess

        workspace = tmp_path / "ws"
        workspace.mkdir()
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "a.txt").write_text("aaa")
        (root_b / "b.txt").write_text("bbb")

        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin", str(root_a), str(root_b)],
            writable_binds=[str(workspace)],
        )
        sb = BubblewrapSandbox(policy)

        cmd = sb.wrap_command(["/bin/sh", "-c", f"cat {root_a}/a.txt && cat {root_b}/b.txt"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert "aaa" in result.stdout
        assert "bbb" in result.stdout

    def test_unmounted_path_not_visible(self, tmp_path: Path):
        """A path NOT in readonly_binds should not be visible inside the sandbox."""
        import subprocess

        workspace = tmp_path / "ws"
        workspace.mkdir()
        secret = tmp_path / "secret"
        secret.mkdir()
        (secret / "key.pem").write_text("top secret")

        # secret is NOT in readonly_binds
        policy = SandboxPolicy(
            readonly_binds=["/usr", "/lib", "/lib64", "/bin", "/sbin"],
            writable_binds=[str(workspace)],
        )
        sb = BubblewrapSandbox(policy)

        cmd = sb.wrap_command(["cat", str(secret / "key.pem")])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "No such file" in result.stderr
