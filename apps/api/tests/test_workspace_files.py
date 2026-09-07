from __future__ import annotations

from pathlib import Path

import pytest
from conftest import register_and_login
from datafoundry_api import app as app_module


@pytest.mark.asyncio
async def test_upload_promote_and_read_workspace_file_across_sessions(client, auth_env: dict[str, str]) -> None:
    login = register_and_login(client)
    user_id = login.get("user", {}).get("id")
    uploaded = client.post(
        "/api/v1/files",
        data={"sessionId": "session-a"},
        files=[("file", ("shared.txt", b"shared across sessions", "text/plain"))],
    )
    assert uploaded.status_code == 201, uploaded.text
    upload_dto = uploaded.json().get("data", {}).get("files", [])[0]
    assert upload_dto.get("scope") == "session"
    assert upload_dto.get("origin") == "uploaded"
    assert upload_dto.get("sessionId") == "session-a"

    duplicate = client.post(
        "/api/v1/files",
        data={"sessionId": "session-b"},
        files=[("file", ("same-content.txt", b"shared across sessions", "text/plain"))],
    )
    assert duplicate.status_code == 201, duplicate.text
    duplicate_dto = duplicate.json().get("data", {}).get("files", [])[0]
    assert duplicate_dto.get("assetId") == upload_dto.get("assetId")
    asset_count = client.app.state.store.fetchone("SELECT COUNT(*) AS count FROM file_assets")
    assert asset_count is not None
    assert dict(asset_count).get("count") == 1

    home = Path(auth_env.get("DATAAGENT_HOME", ""))
    materialized = home / str(user_id) / "session-a" / "shared.txt"
    assert materialized.read_text() == "shared across sessions"
    materialized.write_text("session-local edit")
    original = client.get(f"/api/v1/files/{upload_dto.get('id')}/download")
    assert original.content == b"shared across sessions"
    assert not (home / str(user_id) / "workspace" / "shared.txt").exists()

    promoted = client.post(f"/api/v1/files/{upload_dto.get('id')}/promote")
    assert promoted.status_code == 200, promoted.text
    promoted_dto = promoted.json().get("data", {})
    assert promoted_dto.get("scope") == "workspace"
    assert promoted_dto.get("origin") == "saved"
    assert (home / str(user_id) / "workspace" / "shared.txt").read_text() == "shared across sessions"

    workspace_files = client.get("/api/v1/files?scope=workspace&origin=uploaded,saved")
    listed = workspace_files.json().get("data", {}).get("files", [])
    assert [item.get("id") for item in listed] == [promoted_dto.get("id")]

    identity = client.app.state.auth.authenticate(client.cookies.get("df_session"))
    tools = {tool.name: tool for tool in client.app.state.files.workspace_tools(identity, "session-c")}
    read_result = await tools.get("read_workspace_file").ainvoke({"path": "shared.txt"})
    assert read_result.get("content") == "shared across sessions"
    assert (home / str(user_id) / "session-c").is_dir()

    downloaded = client.get(f"/api/v1/files/{promoted_dto.get('id')}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == b"shared across sessions"


def test_workspace_upload_requires_safe_session_id(client) -> None:
    register_and_login(client)
    response = client.post(
        "/api/v1/files",
        data={"sessionId": "../other-user"},
        files=[("file", ("escape.txt", b"no", "text/plain"))],
    )
    assert response.status_code == 400
    assert response.json().get("error", {}).get("code") == "BAD_REQUEST"


def test_reupload_same_session_filename_reuses_ref_and_replaces_content(client, auth_env: dict[str, str]) -> None:
    login = register_and_login(client)
    user_id = str(login.get("user", {}).get("id", ""))
    first = client.post(
        "/api/v1/files",
        data={"sessionId": "session-a"},
        files=[("file", ("report.txt", b"first version", "text/plain"))],
    )
    second = client.post(
        "/api/v1/files",
        data={"sessionId": "session-a"},
        files=[("file", ("report.txt", b"second version", "text/plain"))],
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_dto = first.json().get("data", {}).get("files", [])[0]
    second_dto = second.json().get("data", {}).get("files", [])[0]
    assert second_dto.get("id") == first_dto.get("id")
    assert second_dto.get("assetId") != first_dto.get("assetId")
    assert client.get(f"/api/v1/files/{second_dto.get('id')}/download").content == b"second version"

    home = Path(auth_env.get("DATAAGENT_HOME", ""))
    materialized = home / user_id / "session-a" / "report.txt"
    assert materialized.read_bytes() == b"second version"
    listed = client.get("/api/v1/files?scope=session&sessionId=session-a").json().get("data", {}).get("files", [])
    assert [item.get("id") for item in listed] == [second_dto.get("id")]

    deleted = client.delete(f"/api/v1/files/{second_dto.get('id')}")
    assert deleted.status_code == 200
    assert not materialized.exists()


def test_upload_batch_is_fully_validated_before_any_file_is_persisted(
    client,
    auth_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = register_and_login(client)
    user_id = str(login.get("user", {}).get("id", ""))
    monkeypatch.setattr(app_module, "_MAX_FILE_UPLOAD_BYTES", 4)

    response = client.post(
        "/api/v1/files",
        data={"sessionId": "session-a"},
        files=[
            ("file", ("first.txt", b"ok", "text/plain")),
            ("file", ("second.txt", b"large", "text/plain")),
        ],
    )

    assert response.status_code == 413
    assert client.get("/api/v1/files?scope=session&sessionId=session-a").json().get("data", {}).get("files", []) == []
    home = Path(auth_env.get("DATAAGENT_HOME", ""))
    assert not (home / user_id / "session-a" / "first.txt").exists()
