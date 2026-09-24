"""Host prompt templates and rendering, separate from native Agent assembly.

Filesystem facts come from the caller; plugin and SubAgent instructions remain
owned by extensions. Only bundled templates are formatted, never inserted text.
The root template owns extension-section layout; the SubAgent renderer retains
its optional block separator. No template engine, config discovery or Skill
loading lives here.
"""

from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path

from dataagent.bootstrap.paths import WorkspaceInput


def _render(name: str, **values: str) -> str:
    template = files(__package__).joinpath(name).read_text(encoding="utf-8")
    return template.rstrip("\n").format(**values)


def render_filesystem_prompt(*, outputs: Path, workspaces: Sequence[WorkspaceInput]) -> str:
    """Render actual session paths, keeping workspace order and an empty list valid."""
    return _render(
        "filesystem.md",
        output_directory=str(outputs),
        input_workspaces="".join(f"- {item.name}: {item.path}\n" for item in workspaces),
    )


def render_agent_prompt(*, filesystem_instructions: str, extension_instructions: str = "") -> str:
    """Fill the root template's extension section without adding layout to its content."""
    return _render(
        "agent.md",
        filesystem_instructions=filesystem_instructions,
        extension_instructions=extension_instructions,
    )


def render_subagent_prompt(
    *, filesystem_instructions: str, subagent_instructions: str | None = None,
) -> str:
    """Add shared path policy without inheriting the root Agent's plugin prompt."""
    return _render(
        "subagent.md",
        filesystem_instructions=filesystem_instructions,
        subagent_instructions=f"\n\n{subagent_instructions}" if subagent_instructions else "",
    )
