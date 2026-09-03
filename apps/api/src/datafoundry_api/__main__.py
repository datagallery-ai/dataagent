from __future__ import annotations

import os

import uvicorn

from datafoundry_api.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    os.environ.setdefault("DEEPAGENTS_RUNTIME_MODEL", "fake" if settings.fake_model else "live")
    uvicorn.run(
        "datafoundry_api.app:create_app",
        host=settings.host,
        port=settings.port,
        factory=True,
        reload=os.environ.get("API_RELOAD") == "1",
    )


if __name__ == "__main__":
    main()
