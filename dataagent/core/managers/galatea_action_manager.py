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
"""Galatea-style ActionManager.

Registers tools from **file paths** (one callable per .py file) and skills
from directories containing a ``SKILL.md``.  Coexists with DataAgent's
``ToolManager`` (class-based ``BaseTool`` registration); the two serve
different agent styles and are intentionally separate.

Import path mapping from galatea source:
  ``from core.modules.action_manager import ActionManager``
  → ``from dataagent.core.managers.galatea_action_manager import ActionManager``
"""

from __future__ import annotations

import importlib.util
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.module import Module


@dataclass
class Skill:
    metadata: str
    path: str


class ActionManager(Module):
    def __init__(self) -> None:
        self._tool_registry: dict[str, Callable] = {}
        self._skill_registry: dict[str, Skill] = {}

    @staticmethod
    def serialize_tool_args(tool_args: dict[str, Any] | None) -> dict[str, Any]:
        """Serialize tool arguments."""

        def _serialize(obj: Any) -> Any:
            if obj is None or isinstance(obj, (bool, int, float, str)):
                return obj
            if isinstance(obj, dict):
                return {k: _serialize(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_serialize(v) for v in obj]
            return f"<{type(obj).__name__}>"

        return _serialize(tool_args) if tool_args else {}

    @staticmethod
    def _load_module(tool_path: str) -> Callable:
        """Load a module from a file path."""
        path = Path(tool_path)
        module_name = path.stem
        spec = importlib.util.spec_from_file_location(module_name, tool_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, module_name)

    @staticmethod
    def _parse_skill_metadata(md_content: str, fallback_name: str) -> tuple[str, str]:
        """Parse skill metadata from a file path."""
        name_match = re.search(r"(?m)^name:\s*(.+?)\s*$", md_content)
        desc_match = re.search(r"(?m)^description:\s*(.+?)\s*$", md_content)
        name = name_match.group(1).strip() if name_match else fallback_name
        description = desc_match.group(1).strip() if desc_match else ""
        return name, description

    @staticmethod
    def _truncate(value: Any, *, limit: int) -> Any:
        """Truncate a value."""
        text = str(value if value is not None else "")
        if len(text) <= limit:
            return value if value is not None else {}
        return (
            text[:limit]
            + "\n\n"
            + f"...(truncated: showing first {limit} chars out of {len(text)} chars."
            + " Reason: very large tool outputs are capped before being returned to the model,"
            + " so they do not flood context or degrade reasoning quality."
            + " If the missing content matters, do not request the same full dump again."
            + " Prefer targeted retrieval instead: rerun the underlying command or query"
            + " with command-side filtering so it returns only the specific field, row,"
            + " match, block, or section you need,"
            + " or use `bash` to inspect only the needed portion directly,"
            + " for example with `head`, `tail`, `rg`, `sed`, or `jq`.)"
        )

    def mount(self, env: Env) -> None:
        for tool in env.tools:
            try:
                self.register_tool(tool)
            except Exception as e:
                logging.error(f"Error registering tool {tool}: {e}")
        for skill in env.skills:
            try:
                self.register_skill(skill)
            except Exception as e:
                logging.error(f"Error registering skill {skill}: {e}")

    def register_tool(self, tool_path: str) -> None:
        """Register a tool from a file path."""
        tool = ActionManager._load_module(tool_path)
        self._tool_registry[tool.__name__] = tool

    def register_load_skill_tool(self) -> None:
        """Register a tool to load a skill."""
        skills_metadata = "\n".join(
            f"""<available_skills>
    <skill>
        <name>{skill_name}</name>
        <description>{skill_info.metadata}</description>
    </skill>
</available_skills>"""
            for skill_name, skill_info in self._skill_registry.items()
        )

        doc = f"""Return SKILL.md content for the specified skill.

A skill is a packaged capability that performs a specific task.
Each skill includes a SKILL.md file describing its purpose, inputs, behavior, and usage.

This function retrieves SKILL.md content and workspace-relative location hints.
It does not execute the skill.

{skills_metadata}

Args:
    skill_name: One of the available skill names listed above.

Returns:
    A text payload including:
    - workspace-relative skill path hint
    - raw SKILL.md content
"""

        def load_skill(skill_name: str) -> str:
            skill = self._skill_registry.get(skill_name)
            if skill is None:
                discovered = self._discover_runtime_skill(skill_name)
                if discovered is None:
                    return f"Skill {skill_name} not found"
                self._skill_registry[skill_name] = discovered
                skill = discovered

            skill_dir = Path(skill.path)
            skill_md_path = skill_dir / "SKILL.md"
            if not skill_md_path.exists():
                return f"SKILL.md not found for skill {skill_name}"

            skill_md = skill_md_path.read_text(encoding="utf-8")
            return (
                f"SKILL_WORKSPACE_PATH: .skills/{skill_dir.name}\n"
                "Resolve relative files against SKILL_WORKSPACE_PATH.\n"
                "SKILL_MD:\n"
                f"{skill_md}"
            )

        load_skill.__name__ = "load_skill"
        load_skill.__doc__ = doc
        self._tool_registry["load_skill"] = load_skill

    def get_tool(self, name: str) -> Callable:
        """Get a tool by name."""
        if name not in self._tool_registry:
            raise ValueError(f"Tool {name} not found")
        return self._tool_registry[name]

    def get_tools(self) -> list[Callable]:
        """Get all tools."""
        return list(self._tool_registry.values())

    def get_skills(self) -> list[Skill]:
        """Get all skills."""
        return list(self._skill_registry.values())

    def register_skill(self, skill_path: str) -> None:
        """Register a skill from a file path."""
        skill_dir = Path(skill_path)
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            raise FileNotFoundError(f"SKILL.md not found for skill {skill_path}")

        md_content = skill_md_path.read_text(encoding="utf-8")
        name, description = ActionManager._parse_skill_metadata(md_content, skill_dir.name)
        if not name:
            name = skill_dir.name
        self._skill_registry[name] = Skill(description, skill_path)

    def call(self, tool_name: str, tool_args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool by name."""
        if tool_name not in self._tool_registry:
            raise ValueError(f"Tool {tool_name} not found")

        tool = self._tool_registry[tool_name]
        try:
            output = tool(**(tool_args or {}))
            return {
                "status": "SUCCESS",
                "tool_name": tool_name,
                "tool_args": ActionManager.serialize_tool_args(tool_args),
                "result": self._truncate(output, limit=16384),
            }
        except Exception as e:
            return {
                "status": "ERROR",
                "tool_name": tool_name,
                "tool_args": ActionManager.serialize_tool_args(tool_args),
                "error": str(e),
            }

    def _discover_runtime_skill(self, skill_name: str) -> Skill | None:
        """Discover a skill by name."""
        cwd = Path.cwd().resolve()
        candidate_roots = [cwd / ".skills", cwd / ".skill", cwd.parent / "skills"]
        for root in candidate_roots:
            if not root.exists() or not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                skill_md_path = child / "SKILL.md"
                if not skill_md_path.exists():
                    continue
                try:
                    md_content = skill_md_path.read_text(encoding="utf-8")
                except Exception:
                    logging.error(f"Failed to read {skill_md_path} for skill discovery", exc_info=True)
                parsed_name, parsed_description = ActionManager._parse_skill_metadata(md_content, child.name)
                names = {child.name}
                if parsed_name:
                    names.add(parsed_name)
                if skill_name not in names:
                    continue
                return Skill(parsed_description, str(child.resolve()))
        return None
