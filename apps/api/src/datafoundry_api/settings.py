from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    session_secret: str
    public_base_url: str
    registration_mode: str
    email_delivery: str
    cookie_path: str
    cookie_secure: bool
    metadata_db_path: Path
    checkpoint_db_path: Path
    llm_model: str
    llm_base_url: str | None
    llm_api_key: str | None
    fake_model: bool

    @property
    def model_configured(self) -> bool:
        return self.fake_model or bool(self.llm_api_key)

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] | None = None) -> Settings:
        source = env if env is not None else os.environ
        public_base_url = (source.get("AUTH_PUBLIC_BASE_URL") or "").strip()
        session_secret = (source.get("AUTH_SESSION_SECRET") or "").strip()
        registration_mode = (source.get("AUTH_REGISTRATION_MODE") or "").strip()
        email_delivery = (source.get("AUTH_EMAIL_DELIVERY") or "smtp").strip() or "smtp"
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
        if parsed.scheme == "http" and not loopback:
            raise ValueError("AUTH_PUBLIC_BASE_URL HTTP is only allowed for loopback hosts.")
        if email_delivery == "test" and not loopback:
            raise ValueError("AUTH_EMAIL_DELIVERY=test is only allowed with a loopback AUTH_PUBLIC_BASE_URL.")

        cookie_path = parsed.path.rstrip("/") or "/"
        storage_root = Path(source.get("STORAGE_ROOT_DIR") or "storage")
        metadata_db = Path(source.get("METADATA_DB_PATH") or storage_root / "metadata" / "workbench.sqlite")
        model_mode = (source.get("DEEPAGENTS_RUNTIME_MODEL") or "").strip().lower()
        api_key = (source.get("LLM_API_KEY") or source.get("OPENAI_API_KEY") or "").strip() or None
        if model_mode == "fake":
            fake_model = True
        elif model_mode == "live":
            fake_model = False
        else:
            fake_model = api_key is None

        return cls(
            host=(source.get("API_HOST") or "127.0.0.1").strip(),
            port=_port(source.get("API_PORT") or "8787"),
            session_secret=session_secret,
            public_base_url=public_base_url.rstrip("/"),
            registration_mode=registration_mode,
            email_delivery=email_delivery,
            cookie_path=cookie_path,
            cookie_secure=parsed.scheme == "https",
            metadata_db_path=metadata_db,
            checkpoint_db_path=metadata_db.with_name("langgraph-checkpoints.sqlite"),
            llm_model=(source.get("LLM_MODEL") or "qwen-plus").strip(),
            llm_base_url=(source.get("LLM_BASE_URL") or "").strip() or None,
            llm_api_key=api_key,
            fake_model=fake_model,
        )


def _port(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 65535:
        raise ValueError("API_PORT must be between 1 and 65535")
    return parsed
