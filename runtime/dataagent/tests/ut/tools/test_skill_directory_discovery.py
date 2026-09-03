"""Native Deep Agents skill source compilation tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from deepagents.backends import StateBackend

from dataagent.core.deepagents.config.skills import SkillConfigCompiler


def _write_skill(root: Path, directory: str, name: str) -> None:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: native skill\n---\n\n# Skill\n",
        encoding="utf-8",
    )


def test_custom_skill_directory_becomes_native_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An absolute compatible custom directory is mounted as a native skill source."""
    custom_root = tmp_path / "custom"
    _write_skill(custom_root, "custom_skill", "custom_skill")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "empty-home"))

    compiled = SkillConfigCompiler(
        {"TOOLS": {"skills": {"custom_dirs": [str(custom_root)]}}},
        StateBackend(),
    ).compile()

    assert compiled.sources == ("/skills/custom-0/",)
    assert len(compiled.permissions) == 1


def test_builtin_skill_allowlist_filters_native_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The legacy builtin allowlist limits directories exposed to Deep Agents."""
    package_root = tmp_path / "package"
    builtin_root = package_root / "actions" / "skills"
    _write_skill(builtin_root, "directory_a", "skill_a")
    _write_skill(builtin_root, "directory_b", "skill_b")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "empty-home"))
    compiler = SkillConfigCompiler(
        {"TOOLS": {"skills": {"builtin": ["skill_a"]}}},
        StateBackend(),
    )
    compiler._package_root = package_root

    compiled = compiler.compile()
    listing = compiled.backend.ls("/skills/builtin/")

    assert compiled.sources == ("/skills/builtin/",)
    assert listing.error is None
    assert [Path(str(entry.get("path", "")).rstrip("/")).name for entry in listing.entries or []] == ["directory_a"]


def test_user_skill_directory_is_mounted_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per-user skills remain discoverable without an explicit user allowlist."""
    user_root = tmp_path / "dataagent-home" / "alice" / "skills"
    _write_skill(user_root, "alice_skill", "alice_skill")
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    compiled = SkillConfigCompiler({"USER_ID": "alice"}, StateBackend()).compile()

    assert compiled.sources == ("/skills/user/",)


@pytest.mark.parametrize("user_id", ["../outside", "/tmp/outside", r"..\outside"])
def test_user_skill_directory_rejects_path_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    user_id: str,
) -> None:
    """A user id cannot escape the managed DataAgent home."""
    monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

    with pytest.raises(ValueError, match="must not contain"):
        SkillConfigCompiler({"USER_ID": user_id}, StateBackend()).compile()
