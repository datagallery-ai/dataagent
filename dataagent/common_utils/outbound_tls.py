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
"""LLM 出站 mTLS 证书能力。

证书材料仍统一来自 ``certificate:`` 段。REST 入站由 ``certificate.inbound_enabled`` 控制；
当前仅当 ``outbound_ssl_services`` 包含 ``llm`` 时，LLM 出站请求启用 mTLS。

``outbound_ssl_services`` 保留为列表，是为了后续扩展其它出站服务时不再改配置结构。
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

# 客户端角色的 certificate_mode：值为 (校验服务端, 出示客户端证书)。
_OUTBOUND_CERT_MODE: dict[int, tuple[bool, bool]] = {
    0: (False, False),
    1: (True, False),
    2: (True, False),
    3: (True, True),
}


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
    """把 ``certificate:`` 段（插值后）下发为 ``DATAAGENT_OUTBOUND_*`` 进程环境变量。

    出站客户端证书必须由 ``client_cert_file``/``client_key_file`` 显式配置，不复用
    入站服务端证书。出站开关由 ``outbound_ssl_services`` 列表控制；下发后清空
    SSLContext 缓存以便重新生效。

    ``certificate`` 缺省时默认清空已有进程环境，避免同进程内不同 agent 串用 TLS
    身份。子 agent 继承主 agent 出站 TLS 时需显式传入
    ``preserve_existing_on_missing=True``。
    """
    if not isinstance(certificate, Mapping):
        if not preserve_existing_on_missing:
            _clear_env()
        reset_cache()
        return

    mapping = {
        ENV_SSL_SERVICES: _normalize_services(certificate.get("outbound_ssl_services")),
        ENV_CA_FILE: certificate.get("outbound_ca_cert_file") or certificate.get("ca_cert_file"),
        ENV_CLIENT_CERT: certificate.get("client_cert_file"),
        ENV_CLIENT_KEY: certificate.get("client_key_file"),
        ENV_CIPHERS: certificate.get("cipher_suites"),
        ENV_MODE: certificate.get("certificate_mode"),
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
    raw = os.getenv(ENV_MODE)
    if raw is None or not str(raw).strip():
        return _DEFAULT_MODE
    try:
        mode = int(str(raw).strip())
    except ValueError:
        mode = -1
    if mode not in _OUTBOUND_CERT_MODE:
        raise ValueError(f"Unsupported certificate_mode={raw}; expected one of {sorted(_OUTBOUND_CERT_MODE)}")
    return mode


@lru_cache(maxsize=1)
def _build_context() -> ssl.SSLContext:
    """构造（并缓存）出站 TLS 材料，不判断任何开关。

    缓存的原因：调用方（如 httpx 版 LLMClient）每次请求新建 client，若每请求都重建
    上下文并 ``load_cert_chain`` 读盘，开销显著。配置变更需调用 :func:`reset_cache`。
    """
    mode = _mode()
    verify_server, present_client_cert = _OUTBOUND_CERT_MODE[mode]
    ca_file = (os.getenv(ENV_CA_FILE) or "").strip()
    client_cert = (os.getenv(ENV_CLIENT_CERT) or "").strip()
    client_key = (os.getenv(ENV_CLIENT_KEY) or "").strip()
    ciphers = (os.getenv(ENV_CIPHERS) or "").strip()

    ctx = ssl.create_default_context(cafile=ca_file or None if verify_server else None)

    if not verify_server:
        # 不校验服务端：必须先关 check_hostname 再设 CERT_NONE。
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED

    if present_client_cert:
        if not (client_cert and client_key):
            raise ValueError(
                f"certificate_mode={mode} (mutual TLS) requires client cert/key; "
                f"set {ENV_CLIENT_CERT} and {ENV_CLIENT_KEY}"
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


def httpx_verify():
    """httpx 的 ``verify`` 取值：启用返回 ``SSLContext``，未启用返回 ``False``。

    注意：客户端证书已注入 ``SSLContext``，httpx 不应再额外传 ``cert=``。
    """
    return _build_context() if outbound_ssl_enabled("llm") else False


def reset_cache() -> None:
    """清空 ``SSLContext`` 缓存（配置/环境变量变更或测试用）。"""
    _build_context.cache_clear()
