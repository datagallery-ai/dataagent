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

from dataagent.common_utils.internal_cert_password import apply_sqlrule_cert_password_env
from dataagent.config.config_manager import ConfigManager

_CONFIG_ENV_NAME = "DATAAGENT_REST_CONFIG"
ENV_INBOUND_SERVER_KEY_PASSWORD = "DATAAGENT_INBOUND_SERVER_KEY_PASSWORD"

# inbound_certificate_mode -> ssl 客户端校验策略。0 与 2 实现等价（均为 CERT_NONE）。
_CERT_MODE_TO_SSL: dict[int, int] = {
    0: ssl.CERT_NONE,
    1: ssl.CERT_OPTIONAL,
    2: ssl.CERT_NONE,
    3: ssl.CERT_REQUIRED,
}
_DEFAULT_INBOUND_MODE = 3
_MODE_TRUE_ALIASES = frozenset({"true"})
_MODE_FALSE_ALIASES = frozenset({"false"})


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


def _parse_inbound_mode(raw: Any) -> int:
    """解析 inbound_certificate_mode：仅 true 当成默认 mode 3，禁止 int(True)==1。

    ``True`` / ``true`` → 3（CERT_REQUIRED）。数字 ``0/1/2/3`` 与 ``"3"``
    保持原义。``False`` / ``false`` 保持 ``int(False)==0``（CERT_NONE），
    不把 mode 字段当成 ``inbound_enabled: false``。其它未识别值当缺省 mode 3。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _DEFAULT_INBOUND_MODE
    if isinstance(raw, bool):
        return _DEFAULT_INBOUND_MODE if raw else 0
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in _MODE_TRUE_ALIASES:
            return _DEFAULT_INBOUND_MODE
        if text in _MODE_FALSE_ALIASES:
            return 0
        try:
            mode = int(text)
        except ValueError:
            return _DEFAULT_INBOUND_MODE
    else:
        try:
            mode = int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_INBOUND_MODE
    if mode not in _CERT_MODE_TO_SSL:
        raise ValueError(f"Unsupported inbound_certificate_mode={mode}; expected one of {sorted(_CERT_MODE_TO_SSL)}")
    return mode


def _user_sql_rules(config_path: str) -> Any:
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        return None
    core = cfg.get("CORE") if isinstance(cfg, dict) else None
    perceptor = core.get("perceptor") if isinstance(core, dict) else None
    return perceptor.get("user_sql_rules") if isinstance(perceptor, dict) else None


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
    """Build uvicorn TLS kwargs from ``certificate``; empty dict means plain HTTP.

    整段不写或 ``certificate: {}`` 视为入站默认开启且 mode 3；缺服务端证/CA 报错。
    ``inbound_certificate_mode: true`` 也当成 mode 3，不得 ``int(True)==1``。
    仅显式 ``inbound_enabled: false`` 才走纯 HTTP，且不校验证件文件。
    """
    cert = load_certificate_config(config_path)
    if not _bool_enabled(cert.get("inbound_enabled"), default=True):
        return {}

    server_cert = cert.get("server_cert_file")
    server_key = cert.get("server_key_file")
    ca_cert = cert.get("ca_cert_file")
    mode = _parse_inbound_mode(cert.get("inbound_certificate_mode", _DEFAULT_INBOUND_MODE))
    cert_reqs = _CERT_MODE_TO_SSL[mode]

    required_files = (
        ("server_cert_file", server_cert),
        ("server_key_file", server_key),
    )
    missing = [name for name, value in required_files if not value]
    if cert_reqs != ssl.CERT_NONE and not ca_cert:
        missing.append("ca_cert_file")
    if missing:
        raise ValueError(f"inbound TLS enabled (inbound_certificate_mode={mode}) but missing: {', '.join(missing)}")

    path_checks = (
        ("server_cert_file", server_cert),
        ("server_key_file", server_key),
        ("ca_cert_file", ca_cert),
    )
    for label, path in path_checks:
        if path and not Path(path).expanduser().is_file():
            raise FileNotFoundError(f"certificate.{label} not found: {path}")

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
    pw = (os.getenv(ENV_INBOUND_SERVER_KEY_PASSWORD) or "").strip() or None
    if pw:
        ssl_kwargs["ssl_keyfile_password"] = pw
    return ssl_kwargs


def main() -> None:
    """Start the DataAgent FastAPI server."""
    args = parse_args()
    config_path = args.config
    os.environ[_CONFIG_ENV_NAME] = config_path
    logger.info(f"Using DataAgent config: {config_path}")

    apply_sqlrule_cert_password_env(_user_sql_rules(config_path))
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
