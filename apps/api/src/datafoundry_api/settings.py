from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    """Process settings for the DataFoundry API and embedded DataAgent runtime."""

    host: str
    port: int
    session_secret: str
    public_base_url: str
    registration_mode: str
    email_delivery: str
    auth_disabled: bool
    secret_master_key: str | None
    cookie_path: str
    cookie_secure: bool
    metadata_db_path: Path
    checkpoint_db_path: Path
    store_db_path: Path
    dataagent_config_path: Path

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] | None = None) -> Settings:
        """Load validated API settings from an environment mapping."""
        source = env if env is not None else os.environ
        api_host = (source.get("API_HOST") or "127.0.0.1").strip()
        public_base_url = (source.get("AUTH_PUBLIC_BASE_URL") or "").strip()
        session_secret = (source.get("AUTH_SESSION_SECRET") or "").strip()
        registration_mode = (source.get("AUTH_REGISTRATION_MODE") or "").strip()
        email_delivery = (source.get("AUTH_EMAIL_DELIVERY") or "smtp").strip() or "smtp"
        auth_disabled = (source.get("AUTH_DISABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
        if len(session_secret) < 32:
            raise ValueError("AUTH_SESSION_SECRET must be at least 32 characters.")
        if not public_base_url:
            raise ValueError("AUTH_PUBLIC_BASE_URL is required.")
        if registration_mode not in {"open", "closed"}:
            raise ValueError("AUTH_REGISTRATION_MODE must be open or closed.")
        if email_delivery not in {"smtp", "test"}:
            raise ValueError("AUTH_EMAIL_DELIVERY must be smtp or test.")

        parsed = urlparse(public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AUTH_PUBLIC_BASE_URL must be a valid absolute URL.")
        host = parsed.hostname or ""
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        api_loopback = api_host in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme == "http" and not loopback:
            raise ValueError("AUTH_PUBLIC_BASE_URL HTTP is only allowed for loopback hosts.")
        if email_delivery == "test" and not loopback:
            raise ValueError("AUTH_EMAIL_DELIVERY=test is only allowed with a loopback AUTH_PUBLIC_BASE_URL.")
        if auth_disabled and (not loopback or not api_loopback):
            raise ValueError("AUTH_DISABLED is only allowed when API_HOST and AUTH_PUBLIC_BASE_URL are loopback.")

        cookie_path = parsed.path.rstrip("/") or "/"
        storage_root = Path(source.get("STORAGE_ROOT_DIR") or "storage")
        metadata_db = Path(source.get("METADATA_DB_PATH") or storage_root / "metadata" / "workbench.sqlite")
        checkpoint_db = Path(
            source.get("LANGGRAPH_CHECKPOINT_DB_PATH") or metadata_db.with_name("langgraph-checkpoints.sqlite")
        )
        store_db = Path(source.get("LANGGRAPH_STORE_DB_PATH") or metadata_db.with_name("langgraph-store.sqlite"))
        dataagent_config = Path(source.get("DATAAGENT_CONFIG_PATH") or _default_dataagent_config_path())

        return cls(
            host=api_host,
            port=_port(source.get("API_PORT") or "8787"),
            session_secret=session_secret,
            public_base_url=public_base_url.rstrip("/"),
            registration_mode=registration_mode,
            email_delivery=email_delivery,
            auth_disabled=auth_disabled,
            secret_master_key=(source.get("SECRET_MASTER_KEY") or "").strip() or None,
            cookie_path=cookie_path,
            cookie_secure=parsed.scheme == "https",
            metadata_db_path=metadata_db,
            checkpoint_db_path=checkpoint_db,
            store_db_path=store_db,
            dataagent_config_path=dataagent_config,
        )


def _port(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 65535:
        raise ValueError("API_PORT must be between 1 and 65535")
    return parsed


def _default_dataagent_config_path() -> Path:
    return Path(__file__).with_name("default_dataagent.yaml")
