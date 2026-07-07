# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Tests for builtin skill directory discovery in ToolManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.core.managers.action_manager.manager import ToolManager


class TestBuiltinSkillDirectoryDiscovery:
    def test_discover_skills_from_root_parses_valid_skill(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "test_skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test_skill\ndescription: A test skill\n---\n\n# Test Skill\n",
            encoding="utf-8",
        )

        tm = ToolManager()
        skills, names = tm.discover_skills_from_root(root=tmp_path, allowlist=None)
        assert len(skills) == 1
        assert skills[0]["name"] == "test_skill"
        assert skills[0]["description"] == "A test skill"
        assert skills[0]["path"] == str(skill_dir)
        assert "test_skill" in names

    def test_discover_skills_from_root_respects_allowlist(self, tmp_path: Path) -> None:
        skill_a = tmp_path / "skill_a"
        skill_a.mkdir()
        (skill_a / "SKILL.md").write_text(
            "---\nname: skill_a\ndescription: Skill A\n---\n\n# A\n",
            encoding="utf-8",
        )
        skill_b = tmp_path / "skill_b"
        skill_b.mkdir()
        (skill_b / "SKILL.md").write_text(
            "---\nname: skill_b\ndescription: Skill B\n---\n\n# B\n",
            encoding="utf-8",
        )

        tm = ToolManager()
        skills, names = tm.discover_skills_from_root(root=tmp_path, allowlist={"skill_a"})
        assert len(skills) == 1
        assert skills[0]["name"] == "skill_a"
        # NOTE: The current implementation adds names before allowlist filtering,
        # so names contains all discovered names regardless of allowlist.
        assert names == {"skill_a", "skill_b"}

    def test_discover_builtin_skills_uses_default_builtin_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        builtin_root = tmp_path / "actions" / "skills"
        skill_dir = builtin_root / "data_analysis_report"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: data_analysis_report\ndescription: builtin skill\n---\n\n# Builtin\n",
            encoding="utf-8",
        )

        from dataagent.utils import runtime_paths

        original_package_path = runtime_paths.dataagent_package_path

        def _fake_package_path(*parts: str) -> Path:
            if parts == ("actions", "skills"):
                return builtin_root
            return original_package_path(*parts)

        monkeypatch.setattr(runtime_paths, "dataagent_package_path", _fake_package_path)

        tm = ToolManager()
        skills = tm._discover_builtin_skills(config={"TOOLS": {"skills": {"custom_dirs": ["actions/skills"]}}})

        assert [skill["name"] for skill in skills] == []

    def test_discover_builtin_skills_nonexistent_directory_handled_gracefully(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from dataagent.utils import runtime_paths

        monkeypatch.setattr(runtime_paths, "dataagent_package_path", lambda *_: tmp_path)

        tm = ToolManager()
        skills = tm._discover_builtin_skills(config={"TOOLS": {"skills": {"custom_dirs": ["does_not_exist"]}}})
        assert [skill["name"] for skill in skills] == []


class TestUserSkillDirectoryDiscovery:
    @staticmethod
    def _write_skill(root: Path, name: str) -> None:
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: user skill\n---\n\n# User Skill\n",
            encoding="utf-8",
        )

    def test_refresh_user_skills_loads_from_user_directory(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        home = tmp_path / "dataagent-home"
        self._write_skill(home / "alice" / "skills", "alice_skill")
        monkeypatch.setenv("DATAAGENT_HOME", str(home))

        skills = ToolManager().refresh_user_skills(user_id="alice")

        assert [skill["name"] for skill in skills] == ["alice_skill"]

    @pytest.mark.parametrize("user_id", ["../outside", "/tmp/outside", r"..\outside"])
    def test_refresh_user_skills_rejects_path_traversal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        user_id: str,
    ) -> None:
        monkeypatch.setenv("DATAAGENT_HOME", str(tmp_path / "dataagent-home"))

        with pytest.raises(ValueError, match="must not contain"):
            ToolManager().refresh_user_skills(user_id=user_id)
