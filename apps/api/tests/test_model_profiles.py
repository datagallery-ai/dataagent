from __future__ import annotations

from conftest import register_and_login


def test_model_profile_crud_encrypts_secret_and_resolves_runtime_selection(client) -> None:
    register_and_login(client)
    created = client.post(
        "/api/v1/model-profiles",
        json={
            "id": "deepseek-ui",
            "name": "DeepSeek UI",
            "description": "Configured from the frontend contract",
            "provider": "openai-compatible",
            "modelName": "deepseek-chat",
            "baseUrl": "https://api.deepseek.com/v1",
            "temperature": 0.2,
            "timeoutMs": 120_000,
            "defaultEnabled": True,
            "credentials": {"apiKey": "profile-test-secret"},
        },
    )
    assert created.status_code == 201
    created_profile = created.json().get("data", {})
    assert created_profile.get("id") == "deepseek-ui"
    assert created_profile.get("hasSecret") is True
    assert created_profile.get("connectionStatus") == "untested"
    assert "profile-test-secret" not in created.text

    secret_row = client.app.state.store.fetchone(
        "SELECT ciphertext FROM encrypted_secrets WHERE ref = ?",
        (created_profile.get("secretRef"),),
    )
    assert secret_row is not None
    assert "profile-test-secret" not in str(dict(secret_row).get("ciphertext", ""))

    identity = client.app.state.auth.authenticate(client.cookies.get("df_session"))
    selection = client.app.state.model_profiles.resolve_model_selection(identity, "deepseek-ui")
    assert selection.cache_key == "deepseek-ui:1"
    assert selection.model_slots is not None
    primary = selection.model_slots.get("chat_model", {})
    params = primary.get("params", {})
    assert params.get("model") == "deepseek-chat"
    assert params.get("temperature") == 0.2
    assert selection.run_timeout_ms == 120_000

    patched = client.patch(
        "/api/v1/model-profiles/deepseek-ui",
        json={"name": "DeepSeek UI v2", "revision": created_profile.get("revision"), "temperature": 0.4},
    )
    assert patched.status_code == 200
    patched_profile = patched.json().get("data", {})
    assert patched_profile.get("revision") == 2
    assert patched_profile.get("temperature") == 0.4

    listed = client.get("/api/v1/model-profiles")
    assert listed.status_code == 200
    assert {profile.get("id") for profile in listed.json().get("data", [])} == {"server-default", "deepseek-ui"}

    deleted = client.delete("/api/v1/model-profiles/deepseek-ui")
    assert deleted.status_code == 200
    assert deleted.json().get("data") == {"deleted": True, "id": "deepseek-ui"}
    assert client.get("/api/v1/model-profiles/deepseek-ui").status_code == 404


def test_model_profile_patch_rejects_stale_revision(client) -> None:
    register_and_login(client)
    created = client.post(
        "/api/v1/model-profiles",
        json={
            "id": "revision-test",
            "name": "Revision Test",
            "provider": "openai-compatible",
            "modelName": "test-model",
            "baseUrl": "https://example.test/v1",
            "defaultEnabled": True,
        },
    )
    assert created.status_code == 201

    stale = client.patch(
        "/api/v1/model-profiles/revision-test",
        json={"name": "Stale", "revision": 999},
    )
    assert stale.status_code == 409
    assert stale.json().get("error", {}).get("code") == "REVISION_CONFLICT"


def test_model_profile_rejects_fallback_cycle(client) -> None:
    register_and_login(client)
    first = client.post(
        "/api/v1/model-profiles",
        json={"id": "first", "name": "First", "modelName": "model-a", "provider": "openai-compatible"},
    )
    second = client.post(
        "/api/v1/model-profiles",
        json={
            "id": "second",
            "name": "Second",
            "modelName": "model-b",
            "provider": "openai-compatible",
            "fallbackProfileId": "first",
        },
    )
    assert first.status_code == 201
    assert second.status_code == 201

    cycle = client.patch(
        "/api/v1/model-profiles/first",
        json={"fallbackProfileId": "second", "revision": first.json().get("data", {}).get("revision")},
    )
    assert cycle.status_code == 400
    assert cycle.json().get("error", {}).get("code") == "MODEL_FALLBACK_CYCLE"
