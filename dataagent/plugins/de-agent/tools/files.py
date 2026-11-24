"""Move session output artifacts without replacing existing files."""

from pathlib import Path

from langchain.tools import ToolRuntime
from utils.evidence import ArtifactPathError, output_file

from dataagent import AgentContext


def rename_file(
    source_path: str, destination_path: str, runtime: ToolRuntime[AgentContext]
) -> dict:
    """Rename a file or directory inside this session's outputs; never overwrite a destination."""
    try:
        source = output_file(runtime.context, source_path)
        destination = output_file(runtime.context, destination_path)
        if source == runtime.context.session.outputs.resolve():
            raise ValueError("Cannot rename the session output root")
        if not source.exists():
            raise ValueError("Source does not exist")
        if destination.exists() or Path(destination).is_symlink():
            raise ValueError("Destination already exists")
        if source.is_dir() and destination.is_relative_to(source):
            raise ValueError("Cannot move a directory into itself")
        if not destination.parent.is_dir():
            raise ValueError("Destination parent directory does not exist")
        source.rename(destination)
        return {"ok": True, "source": str(source), "destination": str(destination)}
    except (ArtifactPathError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
