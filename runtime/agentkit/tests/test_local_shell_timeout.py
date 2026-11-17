"""``LocalShellBackend.execute`` 超时必须清理整个进程组，不能只杀 ``sh``。

回归背景：``start_new_session`` 让 ``sh`` 自成进程组长，``subprocess.run`` 的超时
语义只 SIGKILL 直接子进程，``sh`` 继续拉起的深层子进程（典型：大库重 SQL 的
``sqlite3``）会残留下来、脱离超时管理一直跑到自然结束。修复后超时走 ``killpg``
全组清理（Windows 用 ``taskkill /T``）。本文件用「sh → python → python」三层
真实进程验证。
"""

import shlex
import subprocess
import sys
import time
import uuid

import pytest

from dataagent.core.backends.local_shell import LocalShellBackend

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="进程组语义仅 POSIX；Windows 走 taskkill /T 分支"
)


def _survivors(marker: str) -> list[str]:
    """全机扫描命令行里带 ``marker`` 的存活进程（任何一层都会带上 marker）。"""
    listing = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True
    ).stdout
    return [line.strip() for line in listing.splitlines() if marker in line]


def test_execute_timeout_kills_deep_subprocesses_not_just_shell(tmp_path):
    backend = LocalShellBackend(root_dir=tmp_path, virtual_mode=False)
    marker = "timeout-probe-" + uuid.uuid4().hex[:8]
    # 三层拓扑：sh → python(中层) → python(叶, time.sleep)。marker 同时出现在
    # sh 命令行和两层 python 的 -c 参数里，一次扫描覆盖全部层级。
    leaf = f"import time; time.sleep(300)  # {marker}"
    inner = (
        f"import subprocess, sys; "
        f"subprocess.run([sys.executable, '-c', {leaf!r}])"
    )
    # 末尾的 `; true` 强制 sh 保持前台（不会被 exec 优化成直接子进程），
    # 这样中层/叶 python 都是 sh 之下的深层子进程，才真正复现残留场景的拓扑。
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(inner)}; true"

    result = backend.execute(command, timeout=2)

    assert result.exit_code == 124
    assert "timed out" in result.output
    # give the kernel a moment to clear the process table entries
    time.sleep(0.5)
    survivors = _survivors(marker)
    if survivors:
        # 修复失效时先清场，别把残留进程留给机器，再让断言报出实况。
        subprocess.run(["pkill", "-9", "-f", marker], check=False)
    assert not survivors, f"超时后仍有残留进程: {survivors}"


def test_execute_still_returns_124_and_message_on_timeout(tmp_path):
    """既有契约不回归：超时退出码 124、提示文案区分「默认超时」和「自定义超时」。"""
    slow = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(30)'"

    default_style = LocalShellBackend(root_dir=tmp_path, virtual_mode=False, timeout=0.1).execute(slow)
    assert default_style.exit_code == 124
    assert "timed out after 0.1 seconds" in default_style.output
    assert "(custom timeout)" not in default_style.output
    assert default_style.truncated is False

    custom_style = LocalShellBackend(root_dir=tmp_path, virtual_mode=False).execute(slow, timeout=0.1)
    assert custom_style.exit_code == 124
    assert "timed out after 0.1 seconds (custom timeout)" in custom_style.output


def test_execute_normal_run_unaffected(tmp_path):
    """常规路径不回归：正常退出码、stdout/stderr 合并、截断标记照旧。"""
    backend = LocalShellBackend(root_dir=tmp_path, virtual_mode=False)
    result = backend.execute("printf out; printf err 1>&2; exit 7")
    assert result.exit_code == 7
    assert "out" in result.output and "[stderr] err" in result.output
    assert result.truncated is False
