from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Bind address for the control plane. Auth environment variables are ignored."""

    host: str
    port: int

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] | None = None) -> Settings:
        source = env if env is not None else os.environ
        raw_host = source.get("API_HOST")
        # Public bind is opt-in. An unset host stays on loopback; MR4 owns exposing it.
        return cls(
            host=_bind_host("127.0.0.1" if raw_host is None else raw_host),
            port=_port(source.get("API_PORT") or "8787"),
        )


def _normalize_host(value: str) -> str:
    host = value.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    return host


def _bind_host(value: str) -> str:
    host = _normalize_host(value)
    if not host or any(char.isspace() for char in host) or "/" in host:
        raise ValueError("API_HOST must be a bind address such as 127.0.0.1 or 0.0.0.0.")
    return host


def _port(value: str) -> int:
    try:
        parsed = int(str(value).strip())
    except ValueError as exc:
        raise ValueError("API_PORT must be between 1 and 65535") from exc
    if parsed < 1 or parsed > 65535:
        raise ValueError("API_PORT must be between 1 and 65535")
    return parsed
