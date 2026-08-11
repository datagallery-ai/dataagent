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

# inbound_certificate_mode -> ssl 客户端校验策略。0 与 2 实现等价（均为 CERT_NONE）。
_CERT_MODE_TO_SSL: dict[int, int] = {
    0: ssl.CERT_NONE,
    1: ssl.CERT_OPTIONAL,
    2: ssl.CERT_NONE,
    3: ssl.CERT_REQUIRED,
}


def _bool_enabled(value: Any, *, default: bool = True) -> bool:
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
    """Build uvicorn TLS kwargs from ``certificate``; empty dict means plain HTTP."""
    cert = load_certificate_config(config_path)
    if not cert:
        return {}
    if not _bool_enabled(cert.get("inbound_enabled"), default=True):
        return {}

    server_cert = cert.get("server_cert_file")
    server_key = cert.get("server_key_file")
    required_files = (
        ("server_cert_file", server_cert),
        ("server_key_file", server_key),
    )
    missing = [name for name, value in required_files if not value]
    if missing:
        raise ValueError(f"inbound TLS enabled but missing: {', '.join(missing)}")

    ca_cert = cert.get("ca_cert_file")
    path_checks = (
        ("server_cert_file", server_cert),
        ("server_key_file", server_key),
        ("ca_cert_file", ca_cert),
    )
    for label, path in path_checks:
        if path and not Path(path).expanduser().is_file():
            raise FileNotFoundError(f"certificate.{label} not found: {path}")

    mode = int(cert.get("inbound_certificate_mode", 3))
    cert_reqs = _CERT_MODE_TO_SSL.get(mode)
    if cert_reqs is None:
        raise ValueError(f"Unsupported inbound_certificate_mode={mode}; expected one of {sorted(_CERT_MODE_TO_SSL)}")

    if cert_reqs != ssl.CERT_NONE and not ca_cert:
        raise ValueError(f"inbound TLS enabled (inbound_certificate_mode={mode}) but missing: ca_cert_file")

    ssl_kwargs: dict[str, Any] = {
        "ssl_certfile": server_cert,
        "ssl_keyfile": server_key,
        "ssl_cert_reqs": int(cert_reqs),
    }
    if ca_cert:
        ssl_kwargs["ssl_ca_certs"] = ca_cert
    cipher_suites = cert.get("inbound_cipher_suites")
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
