# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""YAML-to-OpenJiuWen skill adapter tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dataagent.core.deep_agent.adapter import DeepAgentAdapter
from dataagent.core.deep_agent.builders.skills import build_skill_rail
from dataagent.core.deep_agent.spec import DeepAgentBuildSpec, SkillSpec


def _write_skill(root: Path, name: str, description: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _spec(
    tmp_path: Path,
    *,
    builtin_allowlist: frozenset[str] = frozenset({"builtin"}),
) -> SkillSpec:
    return SkillSpec(
        builtin_root=tmp_path / "builtin",
        builtin_allowlist=builtin_allowlist,
        custom_dirs=(tmp_path / "custom",),
        user_root=tmp_path / "user",
    )


class _LocalFs:
    async def read_file(self, path: str, **kwargs):
        return SimpleNamespace(
            code=0,
            data=SimpleNamespace(content=Path(path).read_text(encoding="utf-8")),
        )


class _LocalSysOperation:
    def __init__(self) -> None:
        self._run_config = SimpleNamespace(sandbox_root=[])

    def fs(self) -> _LocalFs:
        return _LocalFs()


def _binding(spec: SkillSpec):
    return build_skill_rail(
        spec,
        sys_operation=_LocalSysOperation(),
        base_read_roots=(spec.builtin_root.parent,),
    )


def test_normalizes_builtin_custom_and_user_skill_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "package"
    user_root = tmp_path / "users" / "alice"
    monkeypatch.setattr(
        "dataagent.core.deep_agent.spec.dataagent_package_path",
        lambda *parts: package_root.joinpath(*parts),
    )
    monkeypatch.setattr(
        "dataagent.core.deep_agent.spec.resolve_user_root",
        lambda **kwargs: user_root,
    )

    spec = DeepAgentBuildSpec.from_config(
        {
            "TOOLS": {
                "skills": {
                    "builtin": ["ontology_service"],
                    "custom_dirs": [
                        "actions/skills",
                        "custom/skills",
                        str(tmp_path / "external"),
                    ],
                }
            }
        }
    ).skills

    assert spec is not None
    assert spec.builtin_root == package_root / "actions" / "skills"
    assert spec.builtin_allowlist == frozenset({"data_analysis_report", "ontology_service"})
    assert spec.custom_dirs == (
        package_root / "custom" / "skills",
        tmp_path / "external",
    )
    assert spec.user_root == user_root / "skills"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"TOOLS": {"skills": []}}, "TOOLS.skills must be a mapping"),
        ({"TOOLS": {"skills": {"builtin": "one"}}}, "TOOLS.skills.builtin must be a list"),
        ({"TOOLS": {"skills": {"custom_dirs": "one"}}}, "TOOLS.skills.custom_dirs must be a list"),
        ({"TOOLS": {"skills": {"user": ["one"]}}}, "TOOLS.skills.user is not supported"),
    ],
)
def test_rejects_invalid_skill_yaml(config: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        DeepAgentBuildSpec.from_config(config)


@pytest.mark.asyncio
async def test_rail_loads_builtin_custom_and_user_with_legacy_priority(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    _write_skill(spec.builtin_root, "builtin", "Built in")
    _write_skill(spec.builtin_root, "disabled", "Not enabled")
    _write_skill(spec.custom_dirs[0], "shared", "Custom wins")
    _write_skill(spec.user_root, "shared", "User loses")
    _write_skill(spec.user_root, "user-only", "User skill")

    binding = _binding(spec)
    assert binding is not None
    skills = await binding.refresh()

    assert [(skill.name, skill.description) for skill in skills] == [
        ("builtin", "Built in"),
        ("shared", "Custom wins"),
        ("user-only", "User skill"),
    ]


@pytest.mark.asyncio
async def test_runtime_refresh_detects_modify_add_and_delete(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    skill_dir = _write_skill(spec.custom_dirs[0], "changing", "Version one")
    binding = _binding(spec)
    assert binding is not None

    first = await binding.refresh()
    assert first[0].description == "Version one"

    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: changing\ndescription: Version two with a different size\n---\n",
        encoding="utf-8",
    )
    _write_skill(spec.custom_dirs[0], "added", "New skill")
    second = await binding.refresh()
    assert {skill.name: skill.description for skill in second} == {
        "added": "New skill",
        "changing": "Version two with a different size",
    }

    skill_md.unlink()
    third = await binding.refresh()
    assert [skill.name for skill in third] == ["added"]


@pytest.mark.asyncio
async def test_jiuwen_before_invoke_automatically_refreshes_changed_skill(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    skill_dir = _write_skill(spec.custom_dirs[0], "automatic", "Version one")
    binding = _binding(spec)
    assert binding is not None

    await binding.rail.before_invoke(SimpleNamespace())
    assert binding.rail.skills_meta[0].description == "Version one"

    (skill_dir / "SKILL.md").write_text(
        "---\nname: automatic\ndescription: Version two from before invoke\n---\n",
        encoding="utf-8",
    )
    await binding.rail.before_invoke(SimpleNamespace())
    assert binding.rail.skills_meta[0].description == "Version two from before invoke"


@pytest.mark.asyncio
async def test_jiuwen_lazy_init_cannot_replace_skill_read_operation(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    _write_skill(spec.builtin_root, "builtin", "Readable outside workspace")
    read_operation = _LocalSysOperation()
    binding = build_skill_rail(
        spec,
        sys_operation=read_operation,
        base_read_roots=(tmp_path / "workspace",),
    )
    assert binding is not None

    primary_operation = _LocalSysOperation()
    primary_operation._run_config.sandbox_root = [str(tmp_path / "workspace")]

    # DeepAgent._ensure_initialized() calls set_sys_operation(primary) for every
    # DeepAgentRail. The DataAgent rail must retain its broader read-only op.
    binding.rail.set_sys_operation(primary_operation)
    await binding.rail.reload_skills()

    assert binding.rail.sys_operation is read_operation
    assert binding.rail.skills_meta[0].description == "Readable outside workspace"
    assert primary_operation._run_config.sandbox_root == [str(tmp_path / "workspace")]


@pytest.mark.asyncio
async def test_runtime_can_replace_custom_dirs_and_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec(tmp_path)
    replacement = tmp_path / "replacement"
    alice_root = tmp_path / "users" / "alice"
    _write_skill(spec.custom_dirs[0], "old-custom", "Old")
    _write_skill(replacement, "new-custom", "New")
    _write_skill(alice_root / "skills", "alice-skill", "Alice")
    monkeypatch.setattr(
        "dataagent.core.deep_agent.builders.skills.resolve_user_root",
        lambda **kwargs: alice_root,
    )

    binding = _binding(spec)
    assert binding is not None
    skills = await binding.refresh(user_id="alice", custom_dirs=[replacement])

    assert [skill.name for skill in skills] == ["new-custom", "alice-skill"]
    assert binding.spec.custom_dirs == (replacement,)
    assert binding.spec.user_root == alice_root / "skills"
    assert binding.rail.sys_operation._run_config.sandbox_root == [
        str(spec.builtin_root.parent),
        str(spec.builtin_root),
        str(replacement),
        str(alice_root / "skills"),
    ]


@pytest.mark.asyncio
async def test_failed_runtime_replacement_rolls_back(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    _write_skill(spec.custom_dirs[0], "healthy", "Healthy")
    broken_root = tmp_path / "broken"
    broken = broken_root / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("# no frontmatter", encoding="utf-8")

    binding = _binding(spec)
    assert binding is not None
    await binding.refresh()

    with pytest.raises(ValueError, match="must contain YAML frontmatter"):
        await binding.refresh(custom_dirs=[broken_root])

    assert binding.spec == spec
    assert [skill.name for skill in binding.rail.skills_meta] == ["healthy"]


def test_adapter_builds_explicit_skill_rail() -> None:
    binding = DeepAgentAdapter({}).build_skill_rail(_LocalSysOperation())

    assert binding is not None
    assert binding.rail.skill_mode == "all"
    assert binding.rail.include_tools is False


@pytest.mark.asyncio
async def test_dataagent_refresh_skills_delegates_to_binding() -> None:
    from dataagent.interface.sdk.agent import DataAgent

    binding = SimpleNamespace(
        refresh=AsyncMock(
            return_value=[
                SimpleNamespace(name="skill", description="desc", directory=Path("/tmp/skill"))
            ]
        )
    )
    agent = DataAgent.__new__(DataAgent)
    agent._deep_agent = object()
    agent._skill_binding = binding

    result = await agent.refresh_skills(user_id="alice", custom_dirs=["/tmp/custom"])

    binding.refresh.assert_awaited_once_with(user_id="alice", custom_dirs=["/tmp/custom"])
    assert result == [{"name": "skill", "description": "desc", "path": "/tmp/skill"}]
