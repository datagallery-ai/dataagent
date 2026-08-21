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
"""REST 入站 TLS：显式 inbound_enabled 开启，默认关闭。"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from dataagent.interface.rest_api.start_service import build_ssl_kwargs


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


def test_missing_certificate_section_keeps_http(tmp_path):
    config = _write_config(tmp_path, "MODEL: {}\n")
    assert build_ssl_kwargs(config) == {}


def test_certificate_present_without_inbound_enabled_keeps_http(tmp_path):
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
    assert build_ssl_kwargs(config) == {}


def test_inbound_enabled_true_builds_ssl_kwargs(tmp_path):
    server_cert, server_key, ca_cert = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_enabled: true
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


def test_inbound_enabled_requires_server_certs(tmp_path):
    config = _write_config(
        tmp_path,
        """
certificate:
  inbound_enabled: true
  ca_cert_file: /tmp/missing-ca.crt
""",
    )
    with pytest.raises(ValueError, match="inbound TLS enabled but missing: server_cert_file"):
        build_ssl_kwargs(config)


def test_inbound_certificate_mode_controls_cert_reqs(tmp_path):
    server_cert, server_key, ca_cert = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_enabled: true
  inbound_certificate_mode: 2
  server_cert_file: {server_cert}
  server_key_file: {server_key}
  ca_cert_file: {ca_cert}
""",
    )
    kwargs = build_ssl_kwargs(config)
    assert kwargs["ssl_cert_reqs"] == int(ssl.CERT_NONE)


def test_inbound_mode3_missing_ca_errors(tmp_path):
    server_cert, server_key, _ = _pem_pair(tmp_path)
    config = _write_config(
        tmp_path,
        f"""
certificate:
  inbound_enabled: true
  inbound_certificate_mode: 3
  server_cert_file: {server_cert}
  server_key_file: {server_key}
""",
    )
    with pytest.raises(ValueError, match="missing: ca_cert_file"):
        build_ssl_kwargs(config)
