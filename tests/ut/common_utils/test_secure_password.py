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
"""SecurePassword：ctypes 原位清零、bytearray 中间副本烧写（bytes 永不改写）、白名单构造、pickle、ssl 端到端回归。"""

from __future__ import annotations

import gc
import pickle
import ssl
import sys

import pytest

import dataagent.common_utils.secure_password as secure_password
from dataagent.common_utils.secure_password import SecurePassword, zero_passwords

_PASSPHRASE = b"test-pass-123"


@pytest.fixture
def encrypted_key_pair(tmp_path):
    """openssl 风格加密私钥对：真实走 PEM 解密通路（不接触网络）。"""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "zeroing-lab")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "server.crt"
    key_file = tmp_path / "server.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.BestAvailableEncryption(_PASSPHRASE),
        )
    )
    return str(cert_file), str(key_file)


def test_zero_clears_buffer_in_place_and_is_idempotent():
    password = SecurePassword("from-om")
    buffer = password.as_bytearray()
    password.zero()
    assert buffer == bytearray(len(buffer))
    assert password.zeroed is True
    assert not password
    password.zero()
    assert password.zeroed is True


def test_use_after_zero_raises_runtime_error():
    password = SecurePassword("from-om")
    password.zero()
    with pytest.raises(RuntimeError, match="already zeroed"):
        password.as_ssl_password()
    with pytest.raises(RuntimeError, match="already zeroed"):
        password.as_bytearray()
    with pytest.raises(RuntimeError, match="already zeroed"):
        password.as_bytes()
    with pytest.raises(RuntimeError, match="already zeroed"):
        password.copy()


def test_copy_buffers_are_independent():
    original = SecurePassword("secret")
    clone = original.copy()
    assert clone is not original
    assert clone.as_bytes() == b"secret"
    original.zero()
    assert clone.as_bytes() == b"secret"
    clone.zero()
    assert clone.zeroed and original.zeroed


def test_str_and_bytes_constructions_are_equivalent():
    assert SecurePassword("from-om").as_bytes() == SecurePassword(b"from-om").as_bytes() == b"from-om"


def test_bytearray_input_is_copied_at_construction():
    raw = bytearray(b"from-om")
    password = SecurePassword(raw)
    assert password.as_bytes() == b"from-om"
    raw[:] = b"XXXXXX"
    assert password.as_bytes() == b"from-om"


def test_memoryview_input_is_accepted():
    assert SecurePassword(memoryview(b"from-om")).as_bytes() == b"from-om"


@pytest.mark.parametrize("value", [123, 4.5, None, ["x"], True])
def test_rejects_non_whitelisted_types(value):
    """bytearray(int) 生成 N 个 NUL 字节而非数字字符串，故构造入参限白名单类型。"""
    with pytest.raises(TypeError, match="cert password"):
        SecurePassword(value)


def test_ascii_whitespace_is_stripped():
    assert SecurePassword("  from-om \n").as_bytes() == b"from-om"


def test_empty_password_is_falsy_but_zeroable():
    password = SecurePassword("   ")
    assert not password
    password.zero()
    assert password.zeroed


def test_zero_passwords_clears_all_and_skips_none():
    first = SecurePassword("a")
    second = SecurePassword("b")
    zero_passwords({"inbound": first, "outbound": second, "unused": None})
    assert first.zeroed and second.zeroed
    zero_passwords({"already": first})


def test_pickle_roundtrip_for_multiprocess_spawn():
    """workers>1 时 uvicorn Multiprocess 以 spawn 传 Config，口令对象必须可 pickle。"""
    password = SecurePassword("spawn-pass")
    clone = pickle.loads(pickle.dumps(password))
    assert clone.as_bytes() == b"spawn-pass"
    assert clone.zeroed is False


def test_end_to_end_correct_password_loads_then_zeroed_fails(encrypted_key_pair):
    """§2 流程固化：正确口令可加载；清零后同一口令不可再解密。"""
    cert_file, key_file = encrypted_key_pair
    password = SecurePassword(_PASSPHRASE.decode())
    buffer = password.as_bytearray()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, keyfile=key_file, password=password.as_ssl_password())

    password.zero()
    assert buffer == bytearray(len(buffer))

    zeroed_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with pytest.raises(ssl.SSLError):
        zeroed_ctx.load_cert_chain(cert_file, keyfile=key_file, password=lambda: buffer)


def test_load_cert_chain_does_not_retain_buffer_reference(encrypted_key_pair):
    """§2 流程固化：load_cert_chain 返回后 ssl 层不滞留口令缓冲引用。"""
    cert_file, key_file = encrypted_key_pair
    password = SecurePassword(_PASSPHRASE.decode())
    buffer = password.as_bytearray()
    before = sys.getrefcount(buffer)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, keyfile=key_file, password=password.as_ssl_password())
    gc.collect()
    assert sys.getrefcount(buffer) == before


def test_burn_clears_bytearray_in_place():
    scratch = bytearray(b"plain-text")
    secure_password._burn(scratch)
    assert scratch == bytearray(len(scratch))


def test_single_byte_inputs_do_not_poison_shared_bytes():
    """回归：单字节入口不得把 CPython 进程级共享的 1 字节 bytes 单例原地清零。

    CPython characters 缓存使 ``str.encode``、``bytes([97])``、bytes 常量命中
    同一单例；旧实现对构造期 bytes 副本原位烧写，SecurePassword("a") 会把
    全进程的 b"a" 永久写成 b"\\x00"。断言参照物一律用 bytearray([int])
    构造（bytearray 从不缓存、内容恒真），避免与被污染单例同源而假绿。
    """
    # bytes 常量与 str.encode 结果同为 CPython characters 缓存单例，
    # 构造前后各验一次其内容即可发现进程级污染
    sentinel_a = b"a"
    sentinel_ws = b" "
    for raw in ("a", " ", memoryview(b"x")):
        password = SecurePassword(raw)
        password.zero()
    assert sentinel_a == bytearray([0x61])
    assert sentinel_ws == bytearray([0x20])
    assert bytes([97]) == bytearray([0x61])
    assert SecurePassword("a").as_bytes() == bytearray([0x61])


def test_construction_does_not_touch_caller_inputs():
    """构造不改动调用方本体：不可变入口原样保留，可写缓冲归调用方生命周期。"""
    text = "caller-owned"
    raw_bytes = b"caller-owned"
    raw_array = bytearray(b"caller-owned")
    SecurePassword(text)
    SecurePassword(raw_bytes)
    SecurePassword(raw_array)
    assert text == "caller-owned"
    assert raw_bytes == b"caller-owned"
    assert raw_array == bytearray(b"caller-owned")


def test_str_construction_routes_intermediates_to_burn(monkeypatch):
    """str 入口且需 strip：仅 strip 前整份复制进烧写名单（str→bytearray 直达，无 bytes 中间副本）。"""
    routed = []
    monkeypatch.setattr(secure_password, "_burn", lambda chunk: routed.append(bytes(chunk)))
    SecurePassword(" secret ")
    assert routed == [b" secret "]  # 只有 strip 前整份复制


def test_clean_bytes_input_has_zero_intermediates(monkeypatch):
    """无空白 bytes 入口：零中间副本，整份复制直接持有。"""
    routed = []
    monkeypatch.setattr(secure_password, "_burn", lambda chunk: routed.append(bytes(chunk)))
    password = SecurePassword(b"clean-secret")
    assert routed == []
    assert password.as_bytes() == b"clean-secret"


def test_copy_has_zero_intermediates(monkeypatch):
    """copy() 重构造：源缓冲已 strip，零中间副本。"""
    routed = []
    monkeypatch.setattr(secure_password, "_burn", lambda chunk: routed.append(bytes(chunk)))
    original = SecurePassword(" dual ")
    routed.clear()  # 原件构造时的烧写与本用例无关
    clone = original.copy()
    assert routed == []
    assert clone.as_bytes() == b"dual"
    assert original.as_bytes() == b"dual"
