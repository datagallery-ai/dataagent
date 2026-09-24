"""Effective environment: process variables over an explicit env file over automatic `.env` files.

Values are returned, never written back to `os.environ`, so concurrent SDK callers cannot
pollute each other.
"""

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from types import MappingProxyType

from dotenv import dotenv_values
from dotenv.parser import parse_stream

RESERVED_ENV = frozenset({"DATAAGENT_HOME", "DATAAGENT_V2_TOKEN", "DATAAGENT_V2_INSTANCE_ID"})
"""Read only from the original process environment; env files cannot redefine them."""


@dataclass(frozen=True)
class Environment:
    values: Mapping[str, str] = field(repr=False)
    files: tuple[Path, ...] = ()


def load_env(paths: tuple[Path, ...], env_file: Path | None = None) -> Environment:
    """Load low-to-high priority automatic files, then the explicit file, then the process."""
    candidates = [*paths, *([env_file] if env_file else [])]
    # A repeated real file keeps its highest-priority position.
    resolved = [path.resolve() for path in candidates]
    values, loaded = {}, []
    for index, path in enumerate(candidates):
        if resolved[index] in resolved[index + 1:]:
            continue
        if not path.exists():
            if path == env_file or path.is_symlink():
                raise ValueError(f"Environment file does not exist: {path}")
            continue
        if not path.is_file():
            raise ValueError(f"Expected an environment file: {path}")
        content = path.read_text(encoding="utf-8")
        for binding in parse_stream(StringIO(content)):
            if binding.error:
                raise ValueError(f"Invalid environment file {path}, line {binding.original.line}")
        parsed = dotenv_values(stream=StringIO(content))
        for name, value in parsed.items():
            if name in RESERVED_ENV:
                safe_path = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(path))
                print(f"Ignoring process-only variable {name} in {safe_path}", file=sys.stderr)
            elif value:  # Empty values mean "undefined" and never shadow a lower layer.
                values[name] = value
        loaded.append(path)
    values.update({key: value for key, value in os.environ.items() if value})
    return Environment(MappingProxyType(values), tuple(loaded))
