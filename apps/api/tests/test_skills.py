from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from conftest import register_and_login

SKILL_MD = b"""---
name: workspace-reader
description: Read and summarize common workspace files.
version: 1.2.0
allowed-tools:
  - list_workspace_files
  - read_workspace_file
---

# Workspace reader

Use the common workspace tools before answering.
"""


def _zip_skill() -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("workspace-reader/SKILL.md", SKILL_MD.replace(b"version: 1.2.0", b"version: 2.0.0"))
        archive.writestr("workspace-reader/references/example.txt", b"example")
    return buffer.getvalue()


def test_skill_upload_install_patch_replace_and_download(client, auth_env: dict[str, str]) -> None:
    login = register_and_login(client)
    user_id = login.get("user", {}).get("id")
    created = client.post(
        "/api/v1/skills",
        data={
            "id": "workspace-reader-id",
            "name": "ignored-form-name",
            "description": "ignored form description",
            "defaultEnabled": "true",
            "defaultMcpIds": "ignored-mcp-binding",
        },
        files={"file": ("SKILL.md", SKILL_MD, "text/markdown")},
    )
    assert created.status_code == 201, created.text
    skill = created.json().get("data", {})
    assert skill.get("name") == "workspace-reader"
    assert skill.get("description") == "Read and summarize common workspace files."
    assert skill.get("allowedTools") == ["list_workspace_files", "read_workspace_file"]
    assert skill.get("validationStatus") == "valid"
    assert skill.get("packageFormat") == "skill-md"
    assert skill.get("defaultMcpIds") == ["ignored-mcp-binding"]

    install = Path(auth_env.get("DATAAGENT_HOME", "")) / str(user_id) / "skills" / "workspace-reader"
    assert (install / "SKILL.md").read_bytes() == SKILL_MD

    identity = client.app.state.auth.authenticate(client.cookies.get("df_session"))
    selection = client.app.state.skills.resolve_selection(identity, ("workspace-reader-id",), "workspace-reader-id")
    assert selection.names == ("workspace-reader",)
    assert selection.active_name == "workspace-reader"

    patched = client.patch(
        "/api/v1/skills/workspace-reader-id",
        json={"name": "cannot-override", "defaultEnabled": False, "revision": skill.get("revision")},
    )
    assert patched.status_code == 200, patched.text
    patched_skill = patched.json().get("data", {})
    assert patched_skill.get("name") == "workspace-reader"
    assert patched_skill.get("defaultEnabled") is False

    zip_content = _zip_skill()
    replaced = client.post(
        "/api/v1/skills/workspace-reader-id/replace",
        files={"file": ("workspace-reader.zip", zip_content, "application/zip")},
    )
    assert replaced.status_code == 200, replaced.text
    replaced_skill = replaced.json().get("data", {})
    assert replaced_skill.get("version") == "2.0.0"
    assert replaced_skill.get("packageFormat") == "zip"
    assert (install / "references" / "example.txt").read_text() == "example"

    validated = client.post("/api/v1/skills/workspace-reader-id/validate")
    assert validated.status_code == 200
    assert validated.json().get("data", {}).get("status") == "valid"

    package = client.get("/api/v1/skills/workspace-reader-id/package")
    assert package.status_code == 200
    assert package.content == zip_content


def test_skill_zip_rejects_path_traversal(client) -> None:
    register_and_login(client)
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", SKILL_MD)
        archive.writestr("../escape.txt", b"escape")
    response = client.post(
        "/api/v1/skills",
        data={"id": "unsafe-skill"},
        files={"file": ("unsafe.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert response.json().get("error", {}).get("code") == "SKILL_ZIP_PATH"


def test_skill_create_rolls_back_install_and_package_ref_when_metadata_write_fails(
    client,
    auth_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = register_and_login(client)
    user_id = str(login.get("user", {}).get("id", ""))
    identity = client.app.state.auth.authenticate(client.cookies.get("df_session"))
    store = client.app.state.store
    execute = store.execute

    def fail_skill_insert(sql: str, params: tuple[object, ...] = ()):
        if "INSERT INTO config_resources" in sql:
            raise RuntimeError("injected metadata failure")
        return execute(sql, params)

    monkeypatch.setattr(store, "execute", fail_skill_insert)
    with pytest.raises(RuntimeError, match="injected metadata failure"):
        client.app.state.skills.create_skill(
            identity,
            filename="SKILL.md",
            content=SKILL_MD,
            fields={"id": "workspace-reader-id"},
        )

    install = Path(auth_env.get("DATAAGENT_HOME", "")) / user_id / "skills" / "workspace-reader"
    assert not install.exists()
    active_refs = store.fetchone(
        "SELECT COUNT(*) AS count FROM file_asset_refs WHERE source = ? AND deleted_at IS NULL",
        ("skill-package",),
    )
    assert active_refs is not None
    assert dict(active_refs).get("count") == 0


def test_skill_replace_restores_previous_install_when_metadata_write_fails(
    client,
    auth_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = register_and_login(client)
    user_id = str(login.get("user", {}).get("id", ""))
    identity = client.app.state.auth.authenticate(client.cookies.get("df_session"))
    skills = client.app.state.skills
    original = skills.create_skill(
        identity,
        filename="SKILL.md",
        content=SKILL_MD,
        fields={"id": "workspace-reader-id"},
    )
    original_ref = original.get("packageFileRefId")
    store = client.app.state.store
    execute = store.execute

    def fail_skill_update(sql: str, params: tuple[object, ...] = ()):
        if "UPDATE config_resources" in sql and "SET name = ?" in sql:
            raise RuntimeError("injected metadata failure")
        return execute(sql, params)

    monkeypatch.setattr(store, "execute", fail_skill_update)
    with pytest.raises(RuntimeError, match="injected metadata failure"):
        skills.replace_skill(
            identity,
            "workspace-reader-id",
            filename="workspace-reader.zip",
            content=_zip_skill(),
        )

    current = skills.get_skill(identity, "workspace-reader-id")
    assert current.get("version") == "1.2.0"
    assert current.get("packageFileRefId") == original_ref
    install = Path(auth_env.get("DATAAGENT_HOME", "")) / user_id / "skills" / "workspace-reader"
    assert (install / "SKILL.md").read_bytes() == SKILL_MD
    assert not (install / "references" / "example.txt").exists()
    active_refs = store.fetchall(
        "SELECT id FROM file_asset_refs WHERE source = ? AND deleted_at IS NULL",
        ("skill-package",),
    )
    assert [dict(row).get("id") for row in active_refs] == [original_ref]
