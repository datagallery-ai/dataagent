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

证书材料来自 ``certificate:`` 段。整段不写或 ``certificate: {}`` 视为默认开启
且 ``outbound_certificate_mode=3``、服务 ``llm,semantic_layer,cloud_core``；
缺客户端证/CA 立即报错（fail-closed），不得回落 ``verify=False`` 或系统 CA。

出站由 ``outbound_enabled``（缺省开启）与 ``outbound_ssl_services``
（缺省 ``llm``+``semantic_layer``+``cloud_core``；显式列表收窄；``[]`` 全关）控制；
校验策略用 ``outbound_certificate_mode``（缺省 3；写 ``true`` 也当成 3）。
显式 ``outbound_enabled: false`` 关闭出站 mTLS，``httpx_verify`` 回落
系统 CA（``True``）；只有 ``mode: 0`` 才允许不校验证书。
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Mapping
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
_DEFAULT_OUTBOUND_SERVICES = ("llm", "semantic_layer", "cloud_core")
_MODE_TRUE_ALIASES = frozenset({"true"})
_MODE_FALSE_ALIASES = frozenset({"false"})

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
    """解析 outbound_certificate_mode：仅 true 当成默认 mode 3，禁止 int(True)==1。

    ``True`` / ``true`` → 3。数字 ``0/1/2/3`` 与 ``"3"`` 保持原义。
    ``False`` / ``false`` 按现有策略拒绝，不把 mode 字段当成整侧关闭。
    其它未识别值当缺省 mode 3。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _DEFAULT_MODE
    if isinstance(raw, bool):
        if raw:
            return _DEFAULT_MODE
        raise ValueError(
            f"Unsupported outbound_certificate_mode={raw!r}; expected one of {sorted(_OUTBOUND_CERT_MODE)}"
        )
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _MODE_TRUE_ALIASES:
            return _DEFAULT_MODE
        if text in _MODE_FALSE_ALIASES:
            raise ValueError(
                f"Unsupported outbound_certificate_mode={raw!r}; expected one of {sorted(_OUTBOUND_CERT_MODE)}"
            )
        try:
            mode = int(text)
        except ValueError:
            return _DEFAULT_MODE
    else:
        try:
            mode = int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_MODE
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


def _missing_outbound_files(
    mode: int,
    *,
    ca_file: Any,
    client_cert: Any,
    client_key: Any,
) -> list[str]:
    """列出当前 mode 仍缺的出站证书字段名。"""
    verify_server, present_client_cert = _OUTBOUND_CERT_MODE[mode]
    missing: list[str] = []
    if present_client_cert:
        if not client_cert:
            missing.append("client_cert_file")
        if not client_key:
            missing.append("client_key_file")
    if verify_server and present_client_cert and not ca_file:
        missing.append("ca_cert_file")
    return missing


def apply_certificate_config(
    certificate: Mapping[str, Any] | None,
    *,
    preserve_existing_on_missing: bool = False,
    key_password: str | None = None,
) -> None:
    """把 ``certificate:`` 段下发为 ``DATAAGENT_OUTBOUND_*``；出站开启时缺材料立即报错。

    ``None`` / ``{}`` 与整段不写相同：默认出站开 + mode 3。``preserve_existing_on_missing``
    为真时保持现有 env，不套默认、不校验证件。私钥口令只经 ``key_password``
    （内部包结果）直传 ``load_cert_chain``，不进 env。
    """
    if not isinstance(certificate, Mapping) or not certificate:
        if preserve_existing_on_missing:
            reset_cache()
            return
        certificate = {}

    password = str(key_password).strip() if key_password else None

    services = _resolve_ssl_services(certificate)
    mode = _parse_outbound_mode(certificate.get("outbound_certificate_mode", _DEFAULT_MODE))
    ca_file = certificate.get("outbound_ca_cert_file") or certificate.get("ca_cert_file")
    client_cert = certificate.get("client_cert_file")
    client_key = certificate.get("client_key_file")
    ciphers = certificate.get("outbound_cipher_suites")

    if services:
        missing = _missing_outbound_files(mode, ca_file=ca_file, client_cert=client_cert, client_key=client_key)
        if missing:
            raise ValueError(
                f"outbound TLS enabled (outbound_certificate_mode={mode}) but missing: {', '.join(missing)}"
            )
        for label, path in (
            ("ca_cert_file", ca_file),
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
    if services:
        _store_context(_make_context(password=password))


def outbound_ssl_enabled(service: str) -> bool:
    """指定出站服务是否启用 mTLS。"""
    wanted = service.strip().lower()
    if not wanted:
        return False
    services = {item.strip().lower() for item in (os.getenv(ENV_SSL_SERVICES) or "").split(",") if item.strip()}
    return wanted in services


def _mode() -> int:
    return _parse_outbound_mode(os.getenv(ENV_MODE))


_cached_ctx: ssl.SSLContext | None = None


def _store_context(ctx: ssl.SSLContext) -> None:
    global _cached_ctx
    _cached_ctx = ctx


def _make_context(*, password: str | None = None) -> ssl.SSLContext:
    """构造出站 TLS 材料；口令仅经参数传入 ``load_cert_chain``。"""
    mode = _mode()
    verify_server, present_client_cert = _OUTBOUND_CERT_MODE[mode]
    ca_file = (os.getenv(ENV_CA_FILE) or "").strip()
    client_cert = (os.getenv(ENV_CLIENT_CERT) or "").strip()
    client_key = (os.getenv(ENV_CLIENT_KEY) or "").strip()
    ciphers = (os.getenv(ENV_CIPHERS) or "").strip()

    if present_client_cert:
        missing = _missing_outbound_files(mode, ca_file=ca_file, client_cert=client_cert, client_key=client_key)
        if missing:
            raise ValueError(
                f"outbound TLS enabled (outbound_certificate_mode={mode}) but missing: {', '.join(missing)}"
            )
        for label, path in (
            ("ca_cert_file", ca_file),
            ("client_cert_file", client_cert),
            ("client_key_file", client_key),
        ):
            if path and not os.path.isfile(path):
                raise FileNotFoundError(f"certificate.{label} not found: {path}")

    ctx = ssl.create_default_context(cafile=ca_file or None if verify_server else None)

    if not verify_server:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED

    if present_client_cert:
        ctx.load_cert_chain(certfile=client_cert, keyfile=client_key, password=password)

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
    """httpx 的 ``verify``：启用返回 ``SSLContext``，未启用回落系统 CA（``True``）。

    显式关闭出站 mTLS、或服务未列入 ``outbound_ssl_services`` 时不得返回
    ``False``（关校验）。只有 ``outbound_certificate_mode=0`` 才允许不校验证书。

    ``service`` 默认 ``llm``；Semantic Layer 传 ``semantic_layer``；CloudCore SQL 传 ``cloud_core``。
    客户端证书已注入 ``SSLContext``，httpx 不应再传 ``cert=``。
    """
    if not outbound_ssl_enabled(service):
        return True
    global _cached_ctx
    if _cached_ctx is None:
        _cached_ctx = _make_context()
    return _cached_ctx


def reset_cache() -> None:
    """清空 ``SSLContext`` 缓存（配置/环境变量变更或测试用）。"""
    global _cached_ctx
    _cached_ctx = None
