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
from __future__ import annotations

import argparse
import os
import ssl
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from loguru import logger

from dataagent.config.config_manager import ConfigManager

_CONFIG_ENV_NAME = "DATAAGENT_REST_CONFIG"

# certificate_mode 数值 -> Python ssl 客户端校验策略。
# 数值含义以甲方接口规范为准；确认后只需调整这张表，无需改动其余逻辑。
_CERT_MODE_TO_SSL: dict[int, int] = {
    0: ssl.CERT_NONE,  # 不校验客户端证书
    1: ssl.CERT_OPTIONAL,  # 可选校验客户端证书
    2: ssl.CERT_NONE,  # 单向认证：仅提供服务端证书
    3: ssl.CERT_REQUIRED,  # 双向认证(mTLS)：强制校验网元客户端证书
}


def _bool_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return bool(value)


def load_certificate_config(config_path: str) -> dict[str, Any]:
    """Load the interpolated ``certificate`` section from a DataAgent YAML config."""
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"DataAgent config not found: {config_path}") from exc

    if not isinstance(cfg, dict):
        return {}
    cfg = ConfigManager().interpolate_config(cfg)
    cert = cfg.get("certificate") or {}
    return cert if isinstance(cert, dict) else {}


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the DataAgent API server."""
    parser = argparse.ArgumentParser(description="Start the DataAgent FastAPI service.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args()


def build_ssl_kwargs(config_path: str) -> dict[str, Any]:
    """Build uvicorn TLS kwargs from the ``certificate`` section of the YAML config.

    Returns an empty dict when TLS is disabled so the server falls back to plain HTTP.
    Each uvicorn worker loads the certificate independently, so multi-worker mode is
    supported without extra handling.
    """
    cert = load_certificate_config(config_path)
    if not _bool_enabled(cert.get("inbound_enabled")):
        return {}

    server_cert = cert.get("server_cert_file")
    server_key = cert.get("server_key_file")
    if not (server_cert and server_key):
        raise ValueError("certificate.inbound_enabled=true requires server_cert_file and server_key_file")

    ca_cert = cert.get("ca_cert_file")
    for label, path in (("server_cert_file", server_cert), ("server_key_file", server_key), ("ca_cert_file", ca_cert)):
        if path and not Path(path).expanduser().is_file():
            raise FileNotFoundError(f"certificate.{label} not found: {path}")

    mode = int(cert.get("certificate_mode", 3))
    cert_reqs = _CERT_MODE_TO_SSL.get(mode)
    if cert_reqs is None:
        raise ValueError(f"Unsupported certificate_mode={mode}; expected one of {sorted(_CERT_MODE_TO_SSL)}")

    # 双向认证必须提供 CA 用于校验客户端证书，否则握手必然失败，提前拦截给出明确报错。
    if cert_reqs != ssl.CERT_NONE and not ca_cert:
        raise ValueError(f"certificate_mode={mode} requires ca_cert_file to verify client certificates")

    ssl_kwargs: dict[str, Any] = {
        "ssl_certfile": server_cert,
        "ssl_keyfile": server_key,
        "ssl_cert_reqs": int(cert_reqs),
    }
    if ca_cert:
        ssl_kwargs["ssl_ca_certs"] = ca_cert
    cipher_suites = cert.get("cipher_suites")
    if cipher_suites:
        ssl_kwargs["ssl_ciphers"] = cipher_suites
    return ssl_kwargs


def main() -> None:
    """Start the DataAgent FastAPI server."""
    args = parse_args()
    config_path = args.config
    os.environ[_CONFIG_ENV_NAME] = config_path
    logger.info(f"Using DataAgent config: {config_path}")

    ssl_kwargs = build_ssl_kwargs(config_path)
    scheme = "https" if ssl_kwargs else "http"
    logger.info(f"Starting DataAgent service on {scheme}://{args.host}:{args.port} with {args.workers} worker(s)")
    if ssl_kwargs:
        logger.info(f"TLS enabled (ssl_cert_reqs={ssl_kwargs['ssl_cert_reqs']})")

    uvicorn.run(
        "dataagent.interface.rest_api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
