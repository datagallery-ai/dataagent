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


def _enabled(raw: object, *, default: bool = True) -> bool:
    """与 ``start_service._bool_enabled`` / ``outbound_tls._flag_enabled`` 同语义。"""
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in {"1", "true", "on", "yes"}:
            return True
        if text in {"0", "false", "off", "no"}:
            return False
        return default
    return bool(raw)


def _inbound_needs_password(cert: Mapping) -> bool:
    """入站实际会加载 ``server_key_file`` 且 ``inbound_encrypted`` 为开才取口令。

    ``inbound_certificate_mode`` 只控制客户端校验，不是总开关。
    """
    if not _enabled(cert.get("inbound_enabled"), default=True):
        return False
    return _encrypted(cert.get("inbound_encrypted"))


def _outbound_needs_password(cert: Mapping) -> bool:
    """出站实际会 ``load_cert_chain`` 且 ``outbound_encrypted`` 为开才取口令。

    未 opt-in：``outbound_enabled: false`` 或 ``outbound_ssl_services`` 为空。
    mode 1/2 不出示客户端证书，不需要私钥口令。
    """
    from dataagent.common_utils.outbound_tls import (
        _DEFAULT_MODE,
        _OUTBOUND_CERT_MODE,
        _parse_outbound_mode,
        _resolve_ssl_services,
    )

    if not _resolve_ssl_services(cert):
        return False
    mode = _parse_outbound_mode(cert.get("outbound_certificate_mode", _DEFAULT_MODE))
    _verify_server, present_client_cert = _OUTBOUND_CERT_MODE[mode]
    if not present_client_cert:
        return False
    return _encrypted(cert.get("outbound_encrypted"))


def resolve_cert_key_passwords(certificate: object = None) -> dict:
    """按各侧是否实际用私钥 + ``*_encrypted`` 决定是否取内部口令；encrypted 缺省开启。

    某侧 ``*_enabled`` 显式关，或出站 ``outbound_ssl_services`` 为空，该侧不取口令。
    两侧都不需要时 ``return {}``，不调内部包。
    返回 ``{"inbound": pw}`` / ``{"outbound": pw}``，只表示要不要给对应 TLS 带 password。
    不读取 YAML 口令字段。
    """
    cert = certificate if isinstance(certificate, Mapping) else {}
    inbound = _inbound_needs_password(cert)
    outbound = _outbound_needs_password(cert)
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
