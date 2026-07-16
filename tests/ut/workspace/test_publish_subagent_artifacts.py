from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataagent.core.jobs.models import JobResult
from dataagent.core.workspace import publish
from dataagent.core.workspace.publish import (
    ensure_subagent_output_root,
    load_publish_manifest,
    publish_subagent_artifacts,
)


def test_publish_copies_business_artifacts_and_upserts_manifest(tmp_path: Path) -> None:
    source = tmp_path / "subagents" / "sub-a"
    source.mkdir(parents=True)
    (source / "result.md").write_text("ok", encoding="utf-8")
    (source / ".context").mkdir()
    (source / ".context" / "secret.json").write_text("secret", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "data.csv").write_text("id\n1\n", encoding="utf-8")

    published = publish_subagent_artifacts(
        source_workspace=source,
        parent_workspace=tmp_path,
        subagent_session_id="sub-a",
        agent_id="cleaner",
        task="clean input",
        job_id="job-a",
    )

    assert (published / "result.md").read_text(encoding="utf-8") == "ok"
    assert (published / "nested" / "data.csv").is_file()
    assert not (published / ".context").exists()
    manifest = load_publish_manifest(tmp_path / "subagent_output")
    assert manifest["entries"][0]["subagent_id"] == "sub-a"
    assert manifest["entries"][0]["artifacts"] == ["nested/", "result.md"]


def test_publish_missing_manifest_is_empty_and_republish_replaces_entry(tmp_path: Path) -> None:
    root = ensure_subagent_output_root(parent_workspace=tmp_path)
    assert load_publish_manifest(root)["entries"] == []
    source = tmp_path / "subagents" / "sub-a"
    source.mkdir(parents=True)
    (source / "old.txt").write_text("old", encoding="utf-8")
    publish_subagent_artifacts(
        source_workspace=source,
        parent_workspace=tmp_path,
        subagent_session_id="sub-a",
        agent_id="cleaner",
        task="first",
        job_id="job-a",
    )
    (source / "old.txt").unlink()
    (source / "new.txt").write_text("new", encoding="utf-8")
    published = publish_subagent_artifacts(
        source_workspace=source,
        parent_workspace=tmp_path,
        subagent_session_id="sub-a",
        agent_id="cleaner",
        task="second",
        job_id="job-b",
    )
    assert not (published / "old.txt").exists()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["entries"]) == 1
    assert manifest["entries"][0]["job_id"] == "job-b"


def test_publish_uses_configured_output_layout_and_skips_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.md").write_text("ok", encoding="utf-8")
    (source / "outside.txt").write_text("private", encoding="utf-8")
    (source / "linked.txt").symlink_to(source / "outside.txt")
    nested = source / "nested"
    nested.mkdir()
    (nested / "linked.txt").symlink_to(source / "outside.txt")

    published = publish_subagent_artifacts(
        source_workspace=source,
        parent_workspace=tmp_path,
        subagent_session_id="sub-a",
        agent_id="agent",
        task="task",
        job_id="job-a",
        config={"WORKSPACE_POLICY": {"layout": {"subagent_output_dir": "published"}}},
    )

    assert published == tmp_path / "published" / "sub-a"
    assert not (published / "linked.txt").exists()
    assert not (published / "nested" / "linked.txt").exists()


@pytest.mark.parametrize("session_id", ["", "..", "a/b", "a\\b", "/absolute"])
def test_publish_rejects_unsafe_session_id(tmp_path: Path, session_id: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="safe path segment"):
        publish_subagent_artifacts(
            source_workspace=source,
            parent_workspace=tmp_path,
            subagent_session_id=session_id,
            agent_id="agent",
            task="task",
            job_id="job",
        )


def test_job_result_serializes_published_path_for_collect() -> None:
    result = JobResult(
        job_id="job-a",
        agent_id="agent",
        status="completed",
        published_path="/workspace/subagent_output/sub-a",
        published_artifacts=["result.md"],
    )
    payload = result.to_dict()
    assert payload["published_path"] == "/workspace/subagent_output/sub-a"
    assert payload["published_artifacts"] == ["result.md"]


def test_publish_failure_rolls_back_published_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.md").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(publish, "_upsert_manifest", lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        publish_subagent_artifacts(
            source_workspace=source,
            parent_workspace=tmp_path,
            subagent_session_id="sub-a",
            agent_id="agent",
            task="task",
            job_id="job-a",
        )
    assert not (tmp_path / "subagent_output" / "sub-a").exists()
