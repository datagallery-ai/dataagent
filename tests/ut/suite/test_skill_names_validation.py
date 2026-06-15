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
"""Tests for post-merge skill name uniqueness validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataagent.core.suite.validation import validate_unique_skill_names
from dataagent.utils.runtime_paths import dataagent_package_path


def _write_skill(root: Path, folder: str, *, name: str) -> None:
    """Create a minimal skill directory under ``root``."""
    skill_dir = root / folder
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill {name}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_validate_unique_skill_names_accepts_distinct_custom_dirs(tmp_path: Path) -> None:
    """Distinct skill names under separate ``custom_dirs`` roots pass validation."""
    dir_a = tmp_path / "skills_a"
    dir_b = tmp_path / "skills_b"
    _write_skill(dir_a, "skill_a", name="skill_a")
    _write_skill(dir_b, "skill_b", name="skill_b")
    config = {
        "TOOLS": {
            "skills": {
                "custom_dirs": [str(dir_a.resolve()), str(dir_b.resolve())],
            }
        }
    }
    validate_unique_skill_names(config)


def test_validate_unique_skill_names_rejects_duplicate_across_custom_dirs(tmp_path: Path) -> None:
    """Same frontmatter ``name`` under different ``custom_dirs`` roots fails at reload."""
    dir_a = tmp_path / "skills_a"
    dir_b = tmp_path / "skills_b"
    _write_skill(dir_a, "one", name="dup_skill")
    _write_skill(dir_b, "two", name="dup_skill")
    config = {
        "TOOLS": {
            "skills": {
                "custom_dirs": [str(dir_a.resolve()), str(dir_b.resolve())],
            }
        }
    }
    with pytest.raises(ValueError, match="Duplicate skill name 'dup_skill'"):
        validate_unique_skill_names(config)


def test_example_suite_skills_dir_passes_uniqueness_check() -> None:
    """Builtin example_suite ``skills/`` has unique names and validates after merge shape."""
    skills_root = dataagent_package_path("core", "suite", "builtin_suites", "example_suite", "skills")
    config = {
        "TOOLS": {
            "skills": {
                "custom_dirs": [str(skills_root.resolve())],
            }
        }
    }
    validate_unique_skill_names(config)
