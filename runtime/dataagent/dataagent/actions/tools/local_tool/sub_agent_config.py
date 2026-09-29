"""Private temporary configurations for trusted sub-agent wrappers."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from dataagent.utils.runtime_paths import dataagent_home


@contextmanager
def temporary_sub_agent_config(config: dict[str, Any], *, prefix: str, workspace_root: Path) -> Iterator[Path]:
    """Keep a resolved sub-agent config under DataAgent's runtime home, outside the workspace."""
    workspace = workspace_root.expanduser().resolve()
    temp_root = dataagent_home() / "runtime" / "sub_agent_configs"
    if temp_root.resolve().is_relative_to(workspace):
        raise RuntimeError("The runtime config directory must be outside the agent workspace")
    temp_root.mkdir(parents=True, mode=0o700, exist_ok=True)

    with NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix=prefix, dir=temp_root, delete=False, encoding="utf-8"
    ) as temp_file:
        yaml.safe_dump(config, temp_file, allow_unicode=False, sort_keys=False)
        config_path = Path(temp_file.name)
    try:
        yield config_path
    finally:
        config_path.unlink(missing_ok=True)
