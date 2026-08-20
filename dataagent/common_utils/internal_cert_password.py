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

import sys
from collections.abc import Mapping


def _encrypted(raw: object) -> bool:
    """``inbound_encrypted`` / ``outbound_encrypted``：缺省 / None 为开；显式 false/0/off/no 为关。"""
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "off", "no"}
    return bool(raw)


def resolve_cert_key_passwords(certificate: object = None) -> dict:
    """按 inbound/outbound_encrypted 决定是否取内部口令；缺省开启。

    返回 ``{"inbound": pw}`` / ``{"outbound": pw}``，只表示要不要给对应 TLS 带 password。
    不读取 YAML 口令字段。
    """
    cert = certificate if isinstance(certificate, Mapping) else {}
    inbound = _encrypted(cert.get("inbound_encrypted"))
    outbound = _encrypted(cert.get("outbound_encrypted"))
    if not inbound and not outbound:
        return {}
    pw = require_internal_cert_password()
    passwords: dict[str, str] = {}
    if inbound:
        passwords["inbound"] = pw
    if outbound:
        passwords["outbound"] = pw
    return passwords


def require_internal_cert_password() -> str:
    """从 CertManager 取 encryptKeyFilePwdContent，缺包抛 ImportError。"""
    from framework_om import CertManager
    from framework_starter import FrameworkStarter

    original_argv = sys.argv[:]
    if "-processType" not in original_argv:
        sys.argv.extend(["-processType", "nl2sql"])
    try:
        FrameworkStarter.init_framework()
    finally:
        sys.argv = original_argv

    cert_info = CertManager.query_cert_info()
    raw = getattr(cert_info, "encryptKeyFilePwdContent", None)
    if raw is None and isinstance(cert_info, Mapping):
        raw = cert_info.get("encryptKeyFilePwdContent")
    text = str(raw).strip() if raw is not None else ""
    if not text:
        raise ValueError("encryptKeyFilePwdContent")
    return text
