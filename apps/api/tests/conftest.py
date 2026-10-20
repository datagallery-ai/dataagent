from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "runtime" / "agentkit"))
sys.path.insert(0, str(REPO / "apps" / "api" / "src"))


@pytest.fixture
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {
        "API_HOST": "127.0.0.1",
        "API_PORT": "8787",
        "LLM_MODEL": "test-model",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_API_KEY": "test-api-key",
        "DATAAGENT_HOME": str(tmp_path / "home"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("DATAAGENT_CONFIG", "DATAAGENT_WORKSPACE", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return env


@pytest.fixture
async def app(api_env: dict[str, str]):
    del api_env
    from datafoundry_api.app import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as http:
        yield http


def header_names(response: httpx.Response) -> set[str]:
    return {name.lower() for name in response.headers}
