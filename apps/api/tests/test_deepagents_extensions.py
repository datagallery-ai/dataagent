from deepagents_runtime import extra_tools

from datafoundry_api.agent import create_runtime_agent
from datafoundry_api.settings import Settings


def test_api_loads_reserved_deepagents_extensions() -> None:
    assert tuple(extra_tools()) == ()


def test_create_runtime_agent_accepts_empty_extensions(tmp_path) -> None:
    settings = Settings.from_env(
        {
            "AUTH_SESSION_SECRET": "x" * 32,
            "AUTH_PUBLIC_BASE_URL": "http://127.0.0.1:8787",
            "AUTH_REGISTRATION_MODE": "open",
            "AUTH_EMAIL_DELIVERY": "test",
            "DEEPAGENTS_RUNTIME_MODEL": "fake",
            "METADATA_DB_PATH": str(tmp_path / "meta.sqlite"),
        }
    )
    graph = create_runtime_agent(settings)
    assert graph is not None
