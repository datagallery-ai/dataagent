from __future__ import annotations

import os

import uvicorn

from deepagents_runtime.config import RuntimeSettings


def main() -> None:
    settings = RuntimeSettings.from_env()
    os.environ.setdefault("DEEPAGENTS_RUNTIME_MODEL", "fake" if settings.fake_model else "")
    uvicorn.run(
        "deepagents_runtime.app:app",
        host=settings.host,
        port=settings.port,
        factory=False,
    )


if __name__ == "__main__":
    main()
