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
"""出站 mTLS 证书能力。

证书材料来自 ``certificate:`` 段。出站由 ``outbound_enabled``（缺省开启）与
``outbound_ssl_services``（缺省 ``llm``+``semantic_layer``；显式列表收窄；``[]`` 全关）
控制；校验策略用 ``outbound_certificate_mode``（缺省 3）。
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from loguru import logger

ENV_CA_FILE = "DATAAGENT_OUTBOUND_CA_FILE"
ENV_CLIENT_CERT = "DATAAGENT_OUTBOUND_CLIENT_CERT"
ENV_CLIENT_KEY = "DATAAGENT_OUTBOUND_CLIENT_KEY"
ENV_CIPHERS = "DATAAGENT_OUTBOUND_CIPHERS"
ENV_MODE = "DATAAGENT_OUTBOUND_MODE"
ENV_SSL_SERVICES = "DATAAGENT_OUTBOUND_SSL_SERVICES"
ENV_PRESERVE_ON_MISSING = "DATAAGENT_OUTBOUND_TLS_PRESERVE_ON_MISSING"

_DEFAULT_MODE = 3
_DEFAULT_OUTBOUND_SERVICES = ("llm", "semantic_layer")

# outbound_certificate_mode -> (校验服务端, 出示客户端证书)。1 与 2 实现等价。
_OUTBOUND_CERT_MODE: dict[int, tuple[bool, bool]] = {
    0: (False, False),
    1: (True, False),
    2: (True, False),
    3: (True, True),
}


def _flag_enabled(value: Any, *, default: bool = True) -> bool:
    """开关：未写/None 用 default；显式 false/0/off/no 为关。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "on", "yes"}:
            return True
        if text in {"0", "false", "off", "no"}:
            return False
        return default
    return bool(value)


def _normalize_services(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    return ",".join(part.strip().lower() for part in parts if part and part.strip())


def _resolve_ssl_services(certificate: Mapping[str, Any]) -> str:
    if not _flag_enabled(certificate.get("outbound_enabled"), default=True):
        return ""
    if "outbound_ssl_services" not in certificate:
        return ",".join(_DEFAULT_OUTBOUND_SERVICES)
    return _normalize_services(certificate.get("outbound_ssl_services"))


def _parse_outbound_mode(raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _DEFAULT_MODE
    try:
        mode = int(str(raw).strip())
    except ValueError:
        mode = -1
    if mode not in _OUTBOUND_CERT_MODE:
        raise ValueError(
            f"Unsupported outbound_certificate_mode={raw!r}; expected one of {sorted(_OUTBOUND_CERT_MODE)}"
        )
    return mode


def _set_env(name: str, value: Any) -> None:
    if value is None or str(value).strip() == "":
        os.environ.pop(name, None)
    else:
        os.environ[name] = str(value)


def _clear_env() -> None:
    for env_name in (ENV_SSL_SERVICES, ENV_CA_FILE, ENV_CLIENT_CERT, ENV_CLIENT_KEY, ENV_CIPHERS, ENV_MODE):
        os.environ.pop(env_name, None)


def apply_certificate_config(
    certificate: Mapping[str, Any] | None,
    *,
    preserve_existing_on_missing: bool = False,
) -> None:
    """把 ``certificate:`` 段下发为 ``DATAAGENT_OUTBOUND_*``；出站开启时缺材料立即报错。"""
    if not isinstance(certificate, Mapping) or not certificate:
        if not preserve_existing_on_missing:
            _clear_env()
        reset_cache()
        return

    services = _resolve_ssl_services(certificate)
    mode = _parse_outbound_mode(certificate.get("outbound_certificate_mode", _DEFAULT_MODE))
    ca_file = certificate.get("outbound_ca_cert_file") or certificate.get("ca_cert_file")
    client_cert = certificate.get("client_cert_file")
    client_key = certificate.get("client_key_file")
    ciphers = certificate.get("outbound_cipher_suites")

    if services:
        _, present_client_cert = _OUTBOUND_CERT_MODE[mode]
        missing: list[str] = []
        if present_client_cert:
            if not client_cert:
                missing.append("client_cert_file")
            if not client_key:
                missing.append("client_key_file")
        if missing:
            raise ValueError(
                f"outbound TLS enabled (outbound_certificate_mode={mode}) but missing: {', '.join(missing)}"
            )
        for label, path in (
            ("outbound_ca_cert_file/ca_cert_file", ca_file),
            ("client_cert_file", client_cert),
            ("client_key_file", client_key),
        ):
            if path and not os.path.isfile(str(path)):
                raise FileNotFoundError(f"certificate.{label} not found: {path}")

    mapping = {
        ENV_SSL_SERVICES: services,
        ENV_CA_FILE: ca_file,
        ENV_CLIENT_CERT: client_cert,
        ENV_CLIENT_KEY: client_key,
        ENV_CIPHERS: ciphers,
        ENV_MODE: mode,
    }
    for env_name, value in mapping.items():
        _set_env(env_name, value)

    reset_cache()


def outbound_ssl_enabled(service: str) -> bool:
    """指定出站服务是否启用 mTLS。"""
    wanted = service.strip().lower()
    if not wanted:
        return False
    services = {item.strip().lower() for item in (os.getenv(ENV_SSL_SERVICES) or "").split(",") if item.strip()}
    return wanted in services


def _mode() -> int:
    return _parse_outbound_mode(os.getenv(ENV_MODE))


@lru_cache(maxsize=1)
def _build_context() -> ssl.SSLContext:
    """构造（并缓存）出站 TLS 材料，不判断服务开关。"""
    mode = _mode()
    verify_server, present_client_cert = _OUTBOUND_CERT_MODE[mode]
    ca_file = (os.getenv(ENV_CA_FILE) or "").strip()
    client_cert = (os.getenv(ENV_CLIENT_CERT) or "").strip()
    client_key = (os.getenv(ENV_CLIENT_KEY) or "").strip()
    ciphers = (os.getenv(ENV_CIPHERS) or "").strip()

    ctx = ssl.create_default_context(cafile=ca_file or None if verify_server else None)

    if not verify_server:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED

    if present_client_cert:
        if not (client_cert and client_key):
            raise ValueError(
                f"outbound TLS enabled (outbound_certificate_mode={mode}) but missing: "
                "client_cert_file, client_key_file"
            )
        for label, path in ((ENV_CLIENT_CERT, client_cert), (ENV_CLIENT_KEY, client_key)):
            if not os.path.isfile(path):
                raise FileNotFoundError(f"outbound_tls: {label} not found: {path}")
        ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)

    if ciphers:
        ctx.set_ciphers(ciphers)

    logger.debug(
        "outbound_tls: SSLContext built (mode={} ca={} client_cert={})",
        mode,
        bool(ca_file),
        present_client_cert,
    )
    return ctx


def httpx_verify(service: str = "llm"):
    """httpx 的 ``verify``：启用返回 ``SSLContext``，未启用返回 ``False``。

    ``service`` 默认 ``llm``；Semantic Layer 传 ``semantic_layer``。
    客户端证书已注入 ``SSLContext``，httpx 不应再传 ``cert=``。
    """
    return _build_context() if outbound_ssl_enabled(service) else False


def reset_cache() -> None:
    """清空 ``SSLContext`` 缓存（配置/环境变量变更或测试用）。"""
    _build_context.cache_clear()
