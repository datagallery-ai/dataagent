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
"""出站 mTLS 助手 outbound_tls 的单元测试。"""

import os
import ssl

import pytest

from dataagent.common_utils import outbound_tls

_OUTBOUND_ENV_KEYS = (
    outbound_tls.ENV_CA_FILE,
    outbound_tls.ENV_CLIENT_CERT,
    outbound_tls.ENV_CLIENT_KEY,
    outbound_tls.ENV_CIPHERS,
    outbound_tls.ENV_MODE,
    outbound_tls.ENV_SSL_SERVICES,
)


def _hard_clear_outbound_env() -> None:
    """Drop DATAAGENT_OUTBOUND_* from the real process env (not monkeypatch-tracked)."""
    for name in _OUTBOUND_ENV_KEYS:
        os.environ.pop(name, None)


@pytest.fixture
def _cert_files(tmp_path):
    """生成一对自签证书/私钥 + CA（仅用于本测试，不接触网络）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-client")])
    import datetime

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

    cert_file = tmp_path / "client.crt"
    key_file = tmp_path / "client.key"
    ca_file = tmp_path / "ca.crt"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ca_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return {"cert": str(cert_file), "key": str(key_file), "ca": str(ca_file)}


@pytest.fixture(autouse=True)
def _clean_env():
    """清理 DATAAGENT_OUTBOUND_* 并在每个用例前后清空 SSLContext 缓存。

    ``apply_certificate_config`` writes ``os.environ`` directly. Do not rely on
    ``monkeypatch.delenv`` for these keys: its teardown can restore values leaked
    by an earlier test and poison later modules (e.g. llm_manager httpx tests).
    """
    _hard_clear_outbound_env()
    outbound_tls.reset_cache()
    yield
    _hard_clear_outbound_env()
    outbound_tls.reset_cache()


def test_apply_certificate_config_none_defaults_mode3_and_requires_files():
    """整段不写 certificate：出站默认开 + mode 3，缺文件必须明确报错。"""
    with pytest.raises(ValueError, match="outbound_certificate_mode=3") as exc_info:
        outbound_tls.apply_certificate_config(None)
    message = str(exc_info.value)
    assert "client_cert_file" in message
    assert "client_key_file" in message
    assert "ca_cert_file" in message
    assert "server_cert_file" not in message
    assert "server_key_file" not in message
    assert outbound_tls.ENV_SSL_SERVICES not in os.environ
    assert outbound_tls.httpx_verify() is True
    assert outbound_tls.httpx_verify("semantic_layer") is True


def test_apply_certificate_config_empty_mapping_defaults_mode3_and_requires_files():
    """空段 certificate: {} 与整段不写相同：默认 mode 3，缺文件报错。"""
    with pytest.raises(ValueError, match="missing: client_cert_file, client_key_file, ca_cert_file"):
        outbound_tls.apply_certificate_config({})
    assert outbound_tls.ENV_SSL_SERVICES not in os.environ
    assert outbound_tls.httpx_verify() is True


def test_absent_env_is_disabled():
    assert outbound_tls.outbound_ssl_enabled("llm") is False
    assert outbound_tls.outbound_ssl_enabled("metavisor") is False
    assert outbound_tls.outbound_ssl_enabled("semantic_layer") is False


def test_non_llm_tls_helpers_are_not_exposed():
    assert not hasattr(outbound_tls, "service_ssl_context")
    assert not hasattr(outbound_tls, "service_requests_session")


def test_llm_mode3_mutual_tls_builds_context(monkeypatch, _cert_files):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "3")
    monkeypatch.setenv(outbound_tls.ENV_CA_FILE, _cert_files["ca"])
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_CERT, _cert_files["cert"])
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_KEY, _cert_files["key"])

    ctx = outbound_tls.httpx_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_llm_mode0_no_verify(monkeypatch):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "0")

    ctx = outbound_tls.httpx_verify()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def _spy_load_cert_chain(monkeypatch):
    """记录 SSLContext.load_cert_chain 调用，并仍执行真实加载。"""
    calls: list[tuple[tuple, dict]] = []
    original = ssl.SSLContext.load_cert_chain

    def _wrapped(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", _wrapped)
    return calls


def test_llm_mode1_treats_as_server_verify_only(monkeypatch, _cert_files):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "1")
    monkeypatch.setenv(outbound_tls.ENV_CA_FILE, _cert_files["ca"])
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_CERT, _cert_files["cert"])
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_KEY, _cert_files["key"])
    calls = _spy_load_cert_chain(monkeypatch)

    ctx = outbound_tls.httpx_verify()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert calls == []


def test_llm_mode2_skip_verify_but_presents_client_cert(monkeypatch, _cert_files):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "2")
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_CERT, _cert_files["cert"])
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_KEY, _cert_files["key"])
    calls = _spy_load_cert_chain(monkeypatch)

    ctx = outbound_tls.httpx_verify()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE
    assert calls
    assert calls[0][1]["certfile"] == _cert_files["cert"]
    assert calls[0][1]["keyfile"] == _cert_files["key"]


def test_unknown_mode_raises(monkeypatch):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "9")

    with pytest.raises(ValueError, match="Unsupported outbound_certificate_mode='9'"):
        outbound_tls.httpx_verify()


def test_mode3_missing_client_cert_raises(monkeypatch, _cert_files):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "3")
    monkeypatch.setenv(outbound_tls.ENV_CA_FILE, _cert_files["ca"])
    with pytest.raises(ValueError, match="missing: client_cert_file"):
        outbound_tls.httpx_verify()


def test_missing_cert_file_raises(monkeypatch, _cert_files):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "3")
    monkeypatch.setenv(outbound_tls.ENV_CA_FILE, _cert_files["ca"])
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_CERT, "/nonexistent.crt")
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_KEY, "/nonexistent.key")
    with pytest.raises(FileNotFoundError, match="client_cert_file"):
        outbound_tls.httpx_verify()


def test_mode3_missing_ca_file_raises(monkeypatch):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "3")
    with pytest.raises(ValueError, match="missing: client_cert_file, client_key_file, ca_cert_file"):
        outbound_tls.httpx_verify()


def test_invalid_cipher_raises(monkeypatch, _cert_files):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "1")
    monkeypatch.setenv(outbound_tls.ENV_CA_FILE, _cert_files["ca"])
    monkeypatch.setenv(outbound_tls.ENV_CIPHERS, "NOT-A-REAL-CIPHER")
    with pytest.raises(ssl.SSLError):
        outbound_tls.httpx_verify()


def test_context_is_cached(monkeypatch):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "0")
    first = outbound_tls.httpx_verify()
    second = outbound_tls.httpx_verify()
    assert first is second


def test_non_llm_services_do_not_enable_httpx_verify(monkeypatch):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "metavisor,semantic_tool")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "3")

    assert outbound_tls.outbound_ssl_enabled("metavisor") is True
    # 未列入出站 mTLS 的服务回落系统 CA，不用 False 关校验。
    assert outbound_tls.httpx_verify() is True
    assert outbound_tls.httpx_verify("semantic_layer") is True


def test_semantic_layer_httpx_verify_disabled_falls_back_to_system_ca(monkeypatch):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "0")

    assert outbound_tls.outbound_ssl_enabled("semantic_layer") is False
    assert outbound_tls.httpx_verify("semantic_layer") is True
    assert isinstance(outbound_tls.httpx_verify("llm"), ssl.SSLContext)


def test_semantic_layer_httpx_verify_enabled_returns_context(monkeypatch, _cert_files):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "semantic_layer")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "3")
    monkeypatch.setenv(outbound_tls.ENV_CA_FILE, _cert_files["ca"])
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_CERT, _cert_files["cert"])
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_KEY, _cert_files["key"])

    assert outbound_tls.outbound_ssl_enabled("semantic_layer") is True
    ctx = outbound_tls.httpx_verify("semantic_layer")
    assert isinstance(ctx, ssl.SSLContext)
    assert outbound_tls.httpx_verify() is True


def test_apply_certificate_config_downfeeds_env(monkeypatch):
    monkeypatch.delenv(outbound_tls.ENV_SSL_SERVICES, raising=False)
    outbound_tls.apply_certificate_config(
        {
            "outbound_ssl_services": ["llm", "metavisor"],
            "outbound_certificate_mode": 1,
            "outbound_cipher_suites": "ECDHE-RSA-AES128-GCM-SHA256",
        }
    )

    assert os.environ[outbound_tls.ENV_SSL_SERVICES] == "llm,metavisor"
    assert outbound_tls.ENV_CA_FILE not in os.environ
    assert outbound_tls.ENV_CLIENT_CERT not in os.environ
    assert os.environ[outbound_tls.ENV_MODE] == "1"
    assert os.environ[outbound_tls.ENV_CIPHERS] == "ECDHE-RSA-AES128-GCM-SHA256"


def test_apply_certificate_config_client_paths(_cert_files):
    outbound_tls.apply_certificate_config(
        {
            "outbound_ssl_services": "llm",
            "outbound_certificate_mode": 3,
            "ca_cert_file": _cert_files["ca"],
            "client_cert_file": _cert_files["cert"],
            "client_key_file": _cert_files["key"],
        }
    )

    assert os.environ[outbound_tls.ENV_SSL_SERVICES] == "llm"
    assert os.environ[outbound_tls.ENV_CLIENT_CERT] == _cert_files["cert"]
    assert os.environ[outbound_tls.ENV_CLIENT_KEY] == _cert_files["key"]
    assert os.environ[outbound_tls.ENV_MODE] == "3"


def test_apply_certificate_config_mode3_missing_client_errors():
    with pytest.raises(ValueError, match="missing: client_cert_file, client_key_file, ca_cert_file"):
        outbound_tls.apply_certificate_config(
            {
                "outbound_ssl_services": ["llm"],
                "outbound_certificate_mode": 3,
            }
        )


def test_apply_certificate_config_mode2_missing_client_errors():
    with pytest.raises(ValueError, match="missing: client_cert_file, client_key_file"):
        outbound_tls.apply_certificate_config(
            {
                "outbound_ssl_services": ["llm"],
                "outbound_certificate_mode": 2,
            }
        )


def test_apply_certificate_config_rejects_bad_mode():
    with pytest.raises(ValueError, match="Unsupported outbound_certificate_mode"):
        outbound_tls.apply_certificate_config(
            {
                "outbound_ssl_services": ["llm"],
                "outbound_certificate_mode": 9,
            }
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, 3),
        ("true", 3),
        ("True", 3),
        (3, 3),
        ("3", 3),
        (1, 1),
        ("1", 1),
        (0, 0),
        (2, 2),
    ],
)
def test_parse_outbound_mode_true_is_default_mode3(raw, expected):
    """布尔 true 当成默认 mode 3；数字 0/1/2/3 与字符串 3 保持原义。"""
    assert outbound_tls._parse_outbound_mode(raw) == expected


@pytest.mark.parametrize("raw", ["yes", "on"])
def test_parse_outbound_mode_unknown_strings_default_mode3(raw):
    """yes/on 等未识别值当没写该键，走缺省 mode 3。"""
    assert outbound_tls._parse_outbound_mode(raw) == 3


@pytest.mark.parametrize("raw", [False, "false", "False"])
def test_parse_outbound_mode_false_is_rejected_not_disable(raw):
    """mode 写 false 按现有策略拒绝，不得当成 outbound_enabled: false。"""
    with pytest.raises(ValueError, match="Unsupported outbound_certificate_mode"):
        outbound_tls._parse_outbound_mode(raw)


def test_apply_certificate_config_true_mode_is_mode3(_cert_files):
    outbound_tls.apply_certificate_config(
        {
            "outbound_certificate_mode": True,
            "ca_cert_file": _cert_files["ca"],
            "client_cert_file": _cert_files["cert"],
            "client_key_file": _cert_files["key"],
        }
    )
    assert os.environ[outbound_tls.ENV_MODE] == "3"
    ctx = outbound_tls.httpx_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_apply_certificate_config_missing_section_does_not_silently_disable(monkeypatch):
    """不写段且未 preserve：按默认 mode 3 校验，缺文件报错，不得清 env 后 verify=False。"""
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_CA_FILE, "/parent/ca.crt")
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_CERT, "/parent/client.crt")
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_KEY, "/parent/client.key")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "3")

    with pytest.raises(ValueError, match="missing: client_cert_file, client_key_file, ca_cert_file"):
        outbound_tls.apply_certificate_config(None)

    assert os.environ[outbound_tls.ENV_SSL_SERVICES] == "llm"
    assert os.environ[outbound_tls.ENV_MODE] == "3"


def test_apply_certificate_config_missing_section_can_preserve_inherited_env(monkeypatch):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm")
    monkeypatch.setenv(outbound_tls.ENV_CA_FILE, "/parent/ca.crt")
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_CERT, "/parent/client.crt")
    monkeypatch.setenv(outbound_tls.ENV_CLIENT_KEY, "/parent/client.key")
    monkeypatch.setenv(outbound_tls.ENV_MODE, "3")

    outbound_tls.apply_certificate_config(None, preserve_existing_on_missing=True)

    assert os.environ[outbound_tls.ENV_SSL_SERVICES] == "llm"
    assert os.environ[outbound_tls.ENV_CA_FILE] == "/parent/ca.crt"
    assert os.environ[outbound_tls.ENV_CLIENT_CERT] == "/parent/client.crt"
    assert os.environ[outbound_tls.ENV_CLIENT_KEY] == "/parent/client.key"
    assert os.environ[outbound_tls.ENV_MODE] == "3"


def test_apply_certificate_config_empty_services_disables_inherited_tls(monkeypatch):
    monkeypatch.setenv(outbound_tls.ENV_SSL_SERVICES, "llm,metavisor")
    outbound_tls.apply_certificate_config({"outbound_ssl_services": []})

    assert outbound_tls.ENV_SSL_SERVICES not in os.environ
    assert outbound_tls.outbound_ssl_enabled("llm") is False
    assert outbound_tls.outbound_ssl_enabled("metavisor") is False


def test_apply_certificate_config_defaults_all_known_services(monkeypatch):
    outbound_tls.apply_certificate_config({"outbound_certificate_mode": 1})

    assert os.environ[outbound_tls.ENV_SSL_SERVICES] == "llm,semantic_layer,cloud_core"
    assert outbound_tls.outbound_ssl_enabled("llm") is True
    assert outbound_tls.outbound_ssl_enabled("semantic_layer") is True
    assert outbound_tls.outbound_ssl_enabled("cloud_core") is True
    assert outbound_tls.outbound_ssl_enabled("metavisor") is False
    assert isinstance(outbound_tls.httpx_verify("cloud_core"), ssl.SSLContext)


def test_apply_certificate_config_outbound_enabled_false_disables_all(monkeypatch):
    outbound_tls.apply_certificate_config(
        {
            "outbound_enabled": False,
            "outbound_ssl_services": ["llm", "semantic_layer"],
        }
    )

    assert outbound_tls.ENV_SSL_SERVICES not in os.environ
    assert outbound_tls.outbound_ssl_enabled("llm") is False
    assert outbound_tls.outbound_ssl_enabled("semantic_layer") is False
    # 显式关 mTLS 回落系统 CA，只有 mode 0 才允许不校验证书。
    assert outbound_tls.httpx_verify() is True
    assert outbound_tls.httpx_verify("semantic_layer") is True


def test_apply_certificate_config_mode3_with_files_builds_context(_cert_files):
    outbound_tls.apply_certificate_config(
        {
            "ca_cert_file": _cert_files["ca"],
            "client_cert_file": _cert_files["cert"],
            "client_key_file": _cert_files["key"],
        }
    )

    assert os.environ[outbound_tls.ENV_SSL_SERVICES] == "llm,semantic_layer,cloud_core"
    assert os.environ[outbound_tls.ENV_MODE] == "3"
    ctx = outbound_tls.httpx_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert isinstance(outbound_tls.httpx_verify("semantic_layer"), ssl.SSLContext)


def test_apply_certificate_config_falls_back_to_shared_ca(_cert_files):
    outbound_tls.apply_certificate_config(
        {
            "outbound_ssl_services": ["llm"],
            "outbound_certificate_mode": 1,
            "ca_cert_file": _cert_files["ca"],
        }
    )

    assert os.environ[outbound_tls.ENV_CA_FILE] == _cert_files["ca"]


def test_apply_certificate_config_mode2_downfeeds_client_paths(_cert_files):
    outbound_tls.apply_certificate_config(
        {
            "outbound_ssl_services": ["llm"],
            "outbound_certificate_mode": 2,
            "client_cert_file": _cert_files["cert"],
            "client_key_file": _cert_files["key"],
        }
    )

    assert os.environ[outbound_tls.ENV_SSL_SERVICES] == "llm"
    assert os.environ[outbound_tls.ENV_CLIENT_CERT] == _cert_files["cert"]
    assert os.environ[outbound_tls.ENV_CLIENT_KEY] == _cert_files["key"]
    assert os.environ[outbound_tls.ENV_MODE] == "2"


def test_outbound_encrypted_passes_internal_password(monkeypatch, _cert_files):
    from dataagent.common_utils.internal_cert_password import resolve_cert_key_passwords
    from tests.ut.common_utils.test_internal_cert_password import install_fake_internal_cert

    install_fake_internal_cert(monkeypatch, "from-om")
    seen: dict[str, object] = {}
    real_load = ssl.SSLContext.load_cert_chain

    def _capture(self, certfile, keyfile=None, password=None):
        seen["password"] = password
        return real_load(self, certfile, keyfile, password=None)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", _capture)
    cert = {
        "outbound_ssl_services": ["llm"],
        "outbound_certificate_mode": 3,
        "ca_cert_file": _cert_files["ca"],
        "client_cert_file": _cert_files["cert"],
        "client_key_file": _cert_files["key"],
    }
    outbound_tls.apply_certificate_config(
        cert,
        key_password=resolve_cert_key_passwords(cert).get("outbound"),
    )
    ctx = outbound_tls.httpx_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert seen["password"] == "from-om"


def test_outbound_encrypted_false_omits_password(monkeypatch, _cert_files):
    from dataagent.common_utils.internal_cert_password import resolve_cert_key_passwords
    from tests.ut.common_utils.test_internal_cert_password import install_fake_internal_cert

    calls = install_fake_internal_cert(monkeypatch, "from-om")
    seen: dict[str, object] = {}
    real_load = ssl.SSLContext.load_cert_chain

    def _capture(self, certfile, keyfile=None, password=None):
        seen["password"] = password
        return real_load(self, certfile, keyfile, password=None)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", _capture)
    cert = {
        "inbound_encrypted": False,
        "outbound_encrypted": False,
        "outbound_ssl_services": ["llm"],
        "outbound_certificate_mode": 3,
        "ca_cert_file": _cert_files["ca"],
        "client_cert_file": _cert_files["cert"],
        "client_key_file": _cert_files["key"],
    }
    outbound_tls.apply_certificate_config(
        cert,
        key_password=resolve_cert_key_passwords(cert).get("outbound"),
    )
    ctx = outbound_tls.httpx_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert seen["password"] is None
    assert calls["init"] == 0
    assert calls["query"] == 0
