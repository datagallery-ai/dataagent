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
"""REST 入站 TLS：默认开启、显式关闭。"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from dataagent.interface.rest_api.start_service import (
    _parse_inbound_mode,
    build_ssl_kwargs,
    load_certificate_config,
)


def _write_config(tmp_path: Path, body: str) -> str:
    path = tmp_path / "agent.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _pem_pair(tmp_path: Path) -> tuple[str, str, str]:
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lab-server")])
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
    ca_file = tmp_path / "ca.crt"
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_file.write_bytes(cert_pem)
    ca_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    return str(cert_file), str(key_file), str(ca_file)


def test_missing_certificate_section_defaults_mode3_and_requires_files(tmp_path):
    config = _write_config(tmp_path, "MODEL: {}\n")
    assert load_certificate_config(config) == {}
    with pytest.raises(ValueError, match="inbound TLS enabled") as exc_info:
        build_ssl_kwargs(config)
    message = str(exc_info.value)
    assert "server_cert_file" in message
    assert "server_key_file" in message
    assert "ca_cert_file" in message


def test_empty_certificate_section_defaults_mode3_and_requires_files(tmp_path):
    config = _write_config(tmp_path, "certificate: {}\n")
    assert load_certificate_config(config) == {}
    with pytest.raises(ValueError, match="missing: server_cert_file, server_key_file, ca_cert_file"):
        build_ssl_kwargs(config)


def test_inbound_defaults_on_when_certificate_present(tmp_path):
    server_cert, server_key, ca_cert = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  server_cert_file: {server_cert}
  server_key_file: {server_key}
  ca_cert_file: {ca_cert}
  inbound_certificate_mode: 3
""",
    )
    kwargs = build_ssl_kwargs(config)
    assert kwargs["ssl_certfile"] == server_cert
    assert kwargs["ssl_keyfile"] == server_key
    assert kwargs["ssl_ca_certs"] == ca_cert
    assert kwargs["ssl_cert_reqs"] == int(ssl.CERT_REQUIRED)


def test_inbound_enabled_false_keeps_http(tmp_path):
    server_cert, server_key, ca_cert = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_enabled: false
  server_cert_file: {server_cert}
  server_key_file: {server_key}
  ca_cert_file: {ca_cert}
""",
    )
    assert build_ssl_kwargs(config) == {}


def test_inbound_enabled_false_skips_file_checks(tmp_path):
    config = _write_config(
        tmp_path,
        """
certificate:
  inbound_enabled: false
""",
    )
    assert build_ssl_kwargs(config) == {}


def test_inbound_defaults_on_requires_server_certs(tmp_path):
    config = _write_config(
        tmp_path,
        """
certificate:
  ca_cert_file: /tmp/missing-ca.crt
""",
    )
    with pytest.raises(ValueError, match="missing: server_cert_file, server_key_file"):
        build_ssl_kwargs(config)


def test_inbound_certificate_mode_controls_cert_reqs(tmp_path):
    server_cert, server_key, ca_cert = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_certificate_mode: 2
  server_cert_file: {server_cert}
  server_key_file: {server_key}
  ca_cert_file: {ca_cert}
""",
    )
    kwargs = build_ssl_kwargs(config)
    assert kwargs["ssl_cert_reqs"] == int(ssl.CERT_NONE)


@pytest.mark.parametrize(
    ("mode_yaml", "expected_reqs"),
    [
        ("true", ssl.CERT_REQUIRED),
        ("True", ssl.CERT_REQUIRED),
        ('"true"', ssl.CERT_REQUIRED),
        ("3", ssl.CERT_REQUIRED),
        ('"3"', ssl.CERT_REQUIRED),
        ("1", ssl.CERT_OPTIONAL),
        ('"1"', ssl.CERT_OPTIONAL),
        ("false", ssl.CERT_NONE),
        ('"false"', ssl.CERT_NONE),
    ],
)
def test_inbound_certificate_mode_true_is_required_not_optional(tmp_path, mode_yaml, expected_reqs):
    """YAML/Python true 当成默认 mode 3（CERT_REQUIRED），数字 1 仍是 OPTIONAL。"""
    server_cert, server_key, ca_cert = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_certificate_mode: {mode_yaml}
  server_cert_file: {server_cert}
  server_key_file: {server_key}
  ca_cert_file: {ca_cert}
""",
    )
    kwargs = build_ssl_kwargs(config)
    assert kwargs["ssl_cert_reqs"] == int(expected_reqs)
    assert "ssl_certfile" in kwargs


@pytest.mark.parametrize("raw", ["yes", "on"])
def test_parse_inbound_mode_unknown_strings_default_mode3(raw):
    """yes/on 等未识别值当没写该键，走缺省 mode 3。"""
    assert _parse_inbound_mode(raw) == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, 3),
        ("true", 3),
        ("True", 3),
        (False, 0),
        ("false", 0),
        ("False", 0),
        (0, 0),
        (3, 3),
    ],
)
def test_parse_inbound_mode_true_false_and_numbers(raw, expected):
    assert _parse_inbound_mode(raw) == expected


def test_inbound_certificate_mode_quoted_yes_defaults_mode3(tmp_path):
    """YAML 字符串 yes 不读，等于默认 mode 3（CERT_REQUIRED）。"""
    server_cert, server_key, ca_cert = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_certificate_mode: "yes"
  server_cert_file: {server_cert}
  server_key_file: {server_key}
  ca_cert_file: {ca_cert}
""",
    )
    kwargs = build_ssl_kwargs(config)
    assert kwargs["ssl_cert_reqs"] == int(ssl.CERT_REQUIRED)


def test_inbound_mode_false_is_cert_none_not_http_disable(tmp_path):
    """mode 写 false 保持 int(False)==0（CERT_NONE），不得当成 inbound_enabled: false。"""
    server_cert, server_key, ca_cert = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_certificate_mode: false
  server_cert_file: {server_cert}
  server_key_file: {server_key}
  ca_cert_file: {ca_cert}
""",
    )
    kwargs = build_ssl_kwargs(config)
    assert kwargs != {}
    assert kwargs["ssl_certfile"] == server_cert
    assert kwargs["ssl_cert_reqs"] == int(ssl.CERT_NONE)


def test_inbound_mode3_missing_ca_errors(tmp_path):
    server_cert, server_key, _ = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_certificate_mode: 3
  server_cert_file: {server_cert}
  server_key_file: {server_key}
""",
    )
    with pytest.raises(ValueError, match="missing: ca_cert_file"):
        build_ssl_kwargs(config)
