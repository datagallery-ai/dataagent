"""Host prompt templates and rendering, separate from native Agent assembly.

Filesystem facts come from the caller; plugin and SubAgent instructions remain
owned by extensions. Only bundled templates are formatted, never inserted text.
The root template owns extension-section layout; the SubAgent renderer retains
its optional block separator. No template engine, config discovery or Skill
loading lives here.
"""

import sys
from collections.abc import Sequence
from importlib.resources import files
from pathlib import Path

from dataagent.bootstrap.paths import WorkspaceInput

# Injected only on win32; keeps non-Windows renders identical to the shared bullets.
_WINDOWS_HOST_NOTES = """
Windows host paths and cmd.exe:
- File tools require the drive-letter absolute paths shown above (for example `C:/Users/...`). Do not invent virtual mounts such as `/workspace` or `/workspace-0`; workspace names are labels, not mount points.
- `execute` runs under cmd.exe (not bash). Prefer `dir` and `type`. Do not use `ls`, `find`, or `2>/dev/null`. Chain commands with `&` or `&&`; `;` is not a command separator.
""".strip("\n")


def _render(name: str, **values: str) -> str:
    template = files(__package__).joinpath(name).read_text(encoding="utf-8")
    return template.rstrip("\n").format(**values)


def render_filesystem_prompt(*, outputs: Path, workspaces: Sequence[WorkspaceInput]) -> str:
    """Render actual session paths, keeping workspace order and an empty list valid."""
    windows_notes = _WINDOWS_HOST_NOTES if sys.platform == "win32" else ""
    if windows_notes:
        windows_notes = "\n" + windows_notes
    return _render(
        "filesystem.md",
        output_directory=str(outputs),
        input_workspaces="".join(f"- {item.name}: {item.path}\n" for item in workspaces),
        windows_host_notes=windows_notes,
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
