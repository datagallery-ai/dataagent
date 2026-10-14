"""Semantic settings, TLS and dependency wiring without synthetic language models."""

import json
import ssl
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.runtime import Runtime as GraphRuntime
from pydantic import ValidationError

from dataagent import ModelContext, build_agent, create_agent
from dataagent.bootstrap import LaunchOptions, prepare_runtime, refresh_extensions
from dataagent.bootstrap.config_files import load_layers, merge
from dataagent.declarations import HookSpec, PluginSpec
from dataagent.errors import DataAgentError
from dataagent.extensions.compiler import compile_extensions
from dataagent.extensions.hooks import compile_hook
from dataagent.semantic_client import SemanticServiceClient
from dataagent.semantic_client.tls import build_ssl_context
from dataagent.settings import SemanticSettings, SemanticTLSSettings
from restapi.app import create_app


def test_config_layers_merge_auth_and_resolve_tls_paths(tmp_path):
    """Resolve individual certificate fields against their own configuration file."""
    home, explicit = tmp_path / "home", tmp_path / "explicit"
    home.mkdir()
    explicit.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "unused",
                        "base_url": "https://unused.invalid",
                        "api_key": "unused",
                    }
                },
                "semantic": {
                    "base_url": "http://semantic.test",
                    "username": "reader",
                    "timeout": 12,
                    "tls": {"verify_server": True, "ca_file": "ca.pem", "client_cert": "cert.pem"},
                },
            }
        )
    )
    override = explicit / "config.json"
    override.write_text(
        json.dumps(
            {"semantic": {"password": "$env{SEMANTIC_PASSWORD}", "tls": {"client_key": "key.pem"}}}
        )
    )
    settings = merge(load_layers(home, {"SEMANTIC_PASSWORD": "private-password"}, override))
    semantic = settings.semantic
    assert semantic.timeout == 12
    assert semantic.password.get_secret_value() == "private-password"
    assert semantic.tls.ca_file == home / "ca.pem"
    assert semantic.tls.client_cert == home / "cert.pem"
    assert semantic.tls.client_key == explicit / "key.pem"
    assert "private-password" not in repr(settings)


def test_null_semantic_can_disable_or_be_overridden(tmp_path):
    """Support explicit disabling and later re-enabling across configuration layers."""
    home = tmp_path / "home"
    home.mkdir()
    base = {
        "models": {
            "primary": {"name": "unused", "base_url": "https://unused.invalid", "api_key": "unused"}
        }
    }
    automatic = home / "config.json"
    override = tmp_path / "config.json"
    automatic.write_text(json.dumps({**base, "semantic": {"base_url": "http://semantic.test"}}))
    override.write_text(json.dumps({"semantic": None}))
    assert merge(load_layers(home, {}, override)).semantic is None
    automatic.write_text(json.dumps({**base, "semantic": None}))
    override.write_text(json.dumps({"semantic": {"base_url": "http://semantic.test"}}))
    assert merge(load_layers(home, {}, override)).semantic.base_url == "http://semantic.test"


@pytest.mark.parametrize(
    "config",
    [
        {"base_url": "   "},
        {"base_url": "http://semantic.test", "username": "reader"},
        {"base_url": "http://semantic.test", "password": "private-password"},
        {"base_url": "http://semantic.test", "timeout": 0},
        {"base_url": "http://semantic.test", "timeout": float("inf")},
        {"base_url": "http://semantic.test", "tls": {"client_cert": "cert.pem"}},
    ],
)
def test_invalid_semantic_settings_fail_before_client_creation(config):
    """Reject incomplete credentials and certificate pairs without echoing secrets."""
    with pytest.raises(ValidationError) as caught:
        SemanticSettings.model_validate(config)
    assert "private-password" not in str(caught.value)


def test_tls_policy_is_explicit_and_independent_of_process_environment(monkeypatch):
    """Preserve the default and opt into verification without reading legacy globals."""
    monkeypatch.setenv("DATAAGENT_OUTBOUND_SSL_SERVICES", "semantic_layer")
    monkeypatch.setenv("DATAAGENT_OUTBOUND_CA_FILE", "/missing/legacy-ca.pem")
    assert build_ssl_context(SemanticTLSSettings()) is False
    verified = build_ssl_context(SemanticTLSSettings(verify_server=True))
    assert verified.verify_mode == ssl.CERT_REQUIRED and verified.check_hostname
    unverified = build_ssl_context(SemanticTLSSettings(ciphers="DEFAULT"))
    assert unverified.verify_mode == ssl.CERT_NONE and not unverified.check_hostname


def test_missing_tls_material_fails_at_resource_creation(tmp_path):
    """Fail startup when configured certificate material cannot be loaded."""
    settings = SemanticTLSSettings(
        client_cert=tmp_path / "cert.pem", client_key=tmp_path / "key.pem"
    )
    with pytest.raises(FileNotFoundError):
        build_ssl_context(settings)


@pytest.mark.parametrize("event", ["before_agent", "after_agent", "before_model", "after_model"])
def test_state_hooks_receive_shared_client_and_copied_params(event, runtime):
    """Supply client identity without putting live resources in params or Agent state."""
    state = {}
    spec = HookSpec(event=event, entrypoint="hook.py:handle", params={"values": []})
    with SemanticServiceClient("semantic.test") as client:
        context = ModelContext(runtime, semantic_client=client)
        native_runtime = GraphRuntime(context=context)

        def handler(actual_state, actual_runtime, *, params):
            assert actual_state is state and actual_runtime is native_runtime
            assert actual_runtime.context.semantic_client is client
            assert params == {"values": []}
            params.get("values").append("changed")
            return None

        middleware = compile_hook(spec, handler, name="SemanticHook")
        getattr(middleware, event)(state, native_runtime)
        getattr(middleware, event)(state, native_runtime)
    assert state == {}


@pytest.mark.parametrize("event", ["before_tool", "after_tool"])
async def test_tool_hooks_use_native_context_in_sync_and_async_paths(event, runtime):
    """Read shared clients through the existing tool request without signature changes."""
    with SemanticServiceClient("semantic.test") as client:
        context = ModelContext(runtime, semantic_client=client)
        request = SimpleNamespace(
            tool_call={"name": "query"}, runtime=GraphRuntime(context=context)
        )
        result, observed = object(), []

        def before(actual_request, *, params):
            observed.append(actual_request.runtime.context.semantic_client)

        def after(actual_request, actual_result, *, params):
            assert actual_result is result
            observed.append(actual_request.runtime.context.semantic_client)

        async def execute(actual_request):
            return result

        spec = HookSpec(event=event, entrypoint="hook.py:handle")
        adapter = compile_hook(
            spec, before if event == "before_tool" else after, name="SemanticHook"
        )
        assert adapter.wrap_tool_call(request, lambda actual_request: result) is result
        assert await adapter.awrap_tool_call(request, execute) is result
        assert observed == [client, client]


def test_subagent_hooks_use_native_context(tmp_path, runtime):
    """Compile unchanged Hook declarations and supply resources via native Runtime."""
    (tmp_path / "prompt.md").write_text("Inspect semantic context.")
    (tmp_path / "hook.py").write_text(
        "def handle(state, runtime, *, params):\n"
        '    return {"marker": runtime.context.semantic_client.base_url}\n'
    )
    hook = {"event": "before_model", "entrypoint": "hook.py:handle"}
    (tmp_path / "child.json").write_text(
        json.dumps(
            {
                "name": "child",
                "description": "inspect",
                "system_prompt": "prompt.md",
                "hooks": [hook],
            }
        )
    )
    plugin = PluginSpec(id="consumer", hooks=[hook], subagents=["child.json"])
    with SemanticServiceClient("semantic.test") as client:
        compiled = compile_extensions([(tmp_path, plugin)])
        (root_hook,) = compiled.get("middleware")
        (child,) = compiled.get("subagents")
        (child_hook,) = child.get("middleware")
        native_runtime = GraphRuntime(context=ModelContext(runtime, semantic_client=client))
        expected = {"marker": client.base_url}
        assert root_hook.before_model({}, native_runtime) == expected
        assert child_hook.before_model({}, native_runtime) == expected


def test_context_keeps_client_private_and_redacts_borrowed_credentials(runtime):
    """Keep live resources out of serialization while retaining diagnostic redaction."""
    with SemanticServiceClient("semantic.test", auth=("reader", "private-password")) as client:
        context = ModelContext(runtime, semantic_client=client)
        assert context.semantic_client is client
        assert context.model_dump() == {}
        assert context.model_json_schema().get("properties") == {}
        assert "private-password" not in repr(context)
        assert "private-password" in context.redaction_secrets
        assert set(client.redaction_secrets) <= set(context.redaction_secrets)


def _with_semantic(runtime):
    return replace(
        runtime,
        settings=runtime.settings.model_copy(
            update={"semantic": SemanticSettings(base_url="http://semantic.test")}
        ),
    )


@pytest.mark.parametrize("async_scope", [False, True])
async def test_sdk_scope_closes_owned_client_on_error(runtime, async_scope):
    """Close real HTTP pools on exceptional exit from either SDK context manager."""
    agent = create_agent(_with_semantic(runtime))
    client = agent.context.semantic_client
    assert client is not None and not client.client.is_closed
    with pytest.raises(DataAgentError):
        if async_scope:
            async with agent:
                raise DataAgentError(source="internal", fact="stop")
        else:
            with agent:
                raise DataAgentError(source="internal", fact="stop")
    assert client.client.is_closed


def test_sdk_does_not_close_borrowed_client(runtime):
    """Leave shared host resources alive when an individual SDK handle closes."""
    with SemanticServiceClient("semantic.test") as client:
        with create_agent(_with_semantic(runtime), semantic_client=client) as agent:
            assert agent.context.semantic_client is client
        assert not client.client.is_closed
    assert client.client.is_closed


def test_sdk_build_failure_closes_owned_client(runtime, monkeypatch):
    """Release a newly created pool if graph assembly fails before returning a handle."""
    from dataagent import client as sdk

    created = []
    factory = SemanticServiceClient.from_settings

    def record(settings):
        client = factory(settings)
        created.append(client)
        return client

    def fail(*args, **kwargs):
        raise ValueError("assembly failed")

    monkeypatch.setattr(SemanticServiceClient, "from_settings", record)
    monkeypatch.setattr(sdk, "build_agent", fail)
    with pytest.raises(ValueError, match="assembly failed"):
        create_agent(_with_semantic(runtime))
    assert len(created) == 1 and created[0].client.is_closed


def test_low_level_graph_requires_explicit_semantic_context(runtime):
    """Avoid silently creating an unowned connection pool in the raw graph API."""
    with pytest.raises(ValueError, match="use create_agent"):
        build_agent(_with_semantic(runtime))


def test_sdk_without_semantic_configuration_has_no_client(runtime):
    """Keep model-only Agents usable without allocating semantic resources."""
    with create_agent(runtime) as agent:
        assert agent.context.semantic_client is None


def test_rest_startup_failure_closes_semantic_client(runtime, monkeypatch):
    """Close host resources even when startup fails before creating the session cache."""
    import restapi.app as rest_app

    created = []
    factory = SemanticServiceClient.from_settings

    def record(settings):
        client = factory(settings)
        created.append(client)
        return client

    async def fail(*args, **kwargs):
        raise ValueError("discovery failed")

    monkeypatch.setattr(SemanticServiceClient, "from_settings", record)
    monkeypatch.setattr(rest_app, "load_mcp_tools", fail)
    with (
        pytest.raises(ValueError, match="discovery failed"),
        TestClient(create_app(_with_semantic(runtime))),
    ):
        pytest.fail("Startup must fail")
    assert len(created) == 1 and created[0].client.is_closed


def test_rest_host_owns_client_and_refresh_preserves_startup_settings(tmp_path):
    """Keep one client across extension refreshes and close it on REST shutdown."""
    config = tmp_path / "config.json"
    values = {
        "models": {
            "primary": {"name": "unused", "base_url": "https://unused.invalid", "api_key": "unused"}
        },
        "semantic": {
            "base_url": "http://semantic.test",
            "username": "reader",
            "password": "private-password",
        },
    }
    config.write_text(json.dumps(values))
    runtime = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))
    assert "private-password" in runtime.redaction_secrets
    app = create_app(runtime)
    with TestClient(app) as http:
        assert http.get("/healthz").status_code == 200
        client = app.state.agents.semantic_client
        assert not client.client.is_closed
        app.state.agents.max_cached = 1
        _, first, _ = http.portal.call(lambda: app.state.agents.get("first", create=True))
        _, second, _ = http.portal.call(lambda: app.state.agents.get("second", create=True))
        assert first.context is not second.context
        assert first.context.semantic_client is second.context.semantic_client is client
        assert len(app.state.agents.cache) == 1
        first.close()
        assert not client.client.is_closed
        values.get("semantic").update(base_url="http://other.test")
        config.write_text(json.dumps(values))
        refreshed = refresh_extensions(runtime)
        assert refreshed.settings.semantic == runtime.settings.semantic
    assert client.client.is_closed
