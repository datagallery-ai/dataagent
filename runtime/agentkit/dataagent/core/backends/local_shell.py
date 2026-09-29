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
# 本文件中的模型可见文案常量原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""本机磁盘加一次 ``/bin/sh -c``，没有沙箱。

文件操作全部留给 ``FilesystemBackend``。本类只加构造参数、``id`` 和 ``execute``。
``virtual_mode`` 管路径，不管命令。默认环境是一份空 dict，不要写成 ``PATH=""``：
变量未设置时，shell 会用自己编译进去的默认 PATH。

几条和「看起来更合理」相反、但必须留着的行为：

- 空命令比超时校验更早。``execute("", timeout=0)`` 返回错误响应，不抛 ``ValueError``。
- 不继承环境时，传入的 ``env`` 就是那一个对象，不拷贝。继承时是构造那一刻的快照。
- stdout 整段保留，stderr 先整段 ``strip`` 再按行加 ``[stderr]``。两边用换行拼接。
- 截断按字符数、用 ``>``，说明接在切片后面，不占额度。然后才给非零退出码 ``rstrip``。
- 超时丢掉已经写出的内容，退出码字段是 124。文案看调用方有没有传 ``timeout=``。
- ``SystemExit`` 和 ``KeyboardInterrupt`` 继续往外抛。
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

from .filesystem import FilesystemBackend
from .protocol import ExecuteResponse, SandboxBackendProtocol

DEFAULT_EXECUTE_TIMEOUT = 120

_EMPTY_COMMAND = "Error: Command must be a non-empty string."

__all__ = ["DEFAULT_EXECUTE_TIMEOUT", "LocalShellBackend"]


def _reject_nonpositive_timeout(amount) -> None:
    """``<= 0`` 才是 ValueError。``None`` 会在比较时变成 TypeError，不要先拦住。"""
    if amount <= 0:
        raise ValueError(f"timeout must be positive, got {amount}")


def _command_environment(inherit, provided):
    """假值不继承。真值先拷贝父环境，再把调用方给的映射 update 上去。"""
    if inherit:
        snapshot = os.environ.copy()
        if provided is not None:
            snapshot.update(provided)
        return snapshot
    if provided is not None:
        return provided
    return {}


def _prefixed_stderr(stderr) -> list[str]:
    if not stderr:
        return []
    stripped = stderr.strip().split("\n")
    return [f"[stderr] {line}" for line in stripped]


def _merge_streams(stdout, stderr) -> str:
    pieces: list[str] = []
    if stdout:
        pieces.append(stdout)
    pieces.extend(_prefixed_stderr(stderr))
    if not pieces:
        return "<no output>"
    return "\n".join(pieces)


def _clip_output(text: str, limit):
    if len(text) > limit:
        head = text[:limit]
        head += f"\n\n... Output truncated at {limit} bytes."
        return head, True
    return text, False


def _attach_exit_line(text: str, returncode) -> str:
    if returncode != 0:
        return f"{text.rstrip()}\n\nExit code: {returncode}"
    return text


def _timeout_message(seconds, caller_supplied: bool) -> str:
    if caller_supplied:
        return f"Error: Command timed out after {seconds} seconds (custom timeout). The command may be stuck or require more time."
    return f"Error: Command timed out after {seconds} seconds. For long-running commands, re-run using the timeout parameter."


def _failure_message(exc: BaseException) -> str:
    return f"Error executing command ({type(exc).__name__}): {exc}"


class LocalShellBackend(FilesystemBackend, SandboxBackendProtocol):
    """父类负责文件，本类负责在 ``cwd`` 里跑一条不过滤的 shell 命令。"""

    def __init__(self, root_dir: str | Path | None = None, *, virtual_mode: bool = True, timeout: int = DEFAULT_EXECUTE_TIMEOUT, max_output_bytes: int = 100_000, env: dict[str, str] | None = None, inherit_env: bool = False) -> None:
        _reject_nonpositive_timeout(timeout)
        super().__init__(root_dir=root_dir, virtual_mode=virtual_mode, max_file_size_mb=10)
        self._default_timeout = timeout
        self._output_limit = max_output_bytes
        self._command_env = _command_environment(inherit_env, env)
        self._instance_label = "local-" + uuid.uuid4().hex[:8]

    @property
    def id(self) -> str:
        """``local-`` 加 8 位小写十六进制。构造时生成，没有 setter。"""
        return self._instance_label

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """跑一条命令。空串和非字符串立刻返回，不看这次的超时。"""
        if not command or not isinstance(command, str):
            return ExecuteResponse(output=_EMPTY_COMMAND, exit_code=1, truncated=False)

        chosen = self._default_timeout if timeout is None else timeout
        _reject_nonpositive_timeout(chosen)
        launch = {
            "check": False,
            "shell": True,
            "capture_output": True,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "timeout": chosen,
            "env": self._command_env,
            "cwd": str(self.cwd),
            "start_new_session": sys.platform != "win32",
        }
        try:
            completed = subprocess.run(command, **launch)
            merged = _merge_streams(completed.stdout, completed.stderr)
            shown, truncated = _clip_output(merged, self._output_limit)
            shown = _attach_exit_line(shown, completed.returncode)
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=_timeout_message(chosen, timeout is not None),
                exit_code=124,
                truncated=False,
            )
        except Exception as exc:
            return ExecuteResponse(output=_failure_message(exc), exit_code=1, truncated=False)
        return ExecuteResponse(output=shown, exit_code=completed.returncode, truncated=truncated)
