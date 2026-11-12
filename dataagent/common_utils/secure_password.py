# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# ============================================================================
from __future__ import annotations

import ctypes
from collections.abc import Callable, Mapping

_ACCEPTED_TYPES = (str, bytes, bytearray, memoryview)


class SecurePassword:
    """私钥解密口令的可清零持有者。底层 bytearray，``zero()`` 后不可再用。"""

    __slots__ = ("_buffer",)

    def __init__(self, value: str | bytes | bytearray | memoryview) -> None:
        if not isinstance(value, _ACCEPTED_TYPES):
            raise TypeError(f"cert password must be one of {_ACCEPTED_TYPES}, got {type(value).__name__}")
        if isinstance(value, memoryview):
            value = value.tobytes()
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._buffer: bytearray | None = bytearray(value).strip()

    def __bool__(self) -> bool:
        return self._buffer is not None and len(self._buffer) > 0

    @property
    def zeroed(self) -> bool:
        """缓冲是否已被 ``zero()`` 清零。"""
        return self._buffer is None

    def as_ssl_password(self) -> Callable[[], bytearray]:
        """供 ``ssl.SSLContext.load_cert_chain(password=...)`` 使用。

        返回的 callable 返回底层 bytearray 本体，ssl 模块按引用读取，不产生副本。
        """
        buffer = self._require_buffer()

        def _password() -> bytearray:
            return buffer

        return _password

    def as_bytearray(self) -> bytearray:
        """供 uvicorn ``Config.ssl_keyfile_password`` 直接挂载使用。

        uvicorn create_ssl_context 会把它包成 ``lambda: password`` 送入
        load_cert_chain。
        """
        return self._require_buffer()

    def as_bytes(self) -> bytes:
        """仅供测试/断言比较；返回临时副本，不受清零影响。"""
        return bytes(self._require_buffer())

    def copy(self) -> SecurePassword:
        """独立生命周期的一份副本（两侧口令需各自清零时使用）。"""
        return SecurePassword(self._require_buffer())

    def zero(self) -> None:
        """``ctypes.memset`` 原位清零底层数据；重复调用无副作用。"""
        buffer = self._buffer
        if buffer is None:
            return
        ctypes.memset((ctypes.c_char * len(buffer)).from_buffer(buffer), 0, len(buffer))
        self._buffer = None

    def _require_buffer(self) -> bytearray:
        if self._buffer is None:
            raise RuntimeError("cert password buffer already zeroed")
        return self._buffer


def zero_passwords(passwords: Mapping[str, SecurePassword | None]) -> None:
    """清零口令集合；None 与已清零对象跳过。"""
    for password in passwords.values():
        if password is not None:
            password.zero()
