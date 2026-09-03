from __future__ import annotations

import os

import uvicorn

from datafoundry_api.settings import Settings


def main() -> None:
    """Start the DataFoundry API with the embedded DataAgent runtime."""
    settings = Settings.from_env()
    uvicorn.run(
        "datafoundry_api.app:create_app",
        host=settings.host,
        port=settings.port,
        factory=True,
        reload=os.environ.get("API_RELOAD") == "1",
    )


if __name__ == "__main__":
    main()
