"""Copy the named scene file and the shared data-source config into the workspace."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

_SCENE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
SUITE_ROOT = Path(__file__).resolve().parents[3]
SCENES_DIR = SUITE_ROOT / "config" / "scenes"
DATA_SOURCE = SUITE_ROOT / "config" / "data_source.md"


def materialize(scene_name: str, dest_dir: Path) -> tuple[Path, Path]:
    name = str(scene_name or "").strip()
    if not _SCENE_NAME.fullmatch(name):
        raise ValueError(f"scene_name must match a file in config/scenes: {scene_name!r}")
    source = SCENES_DIR / f"{name}.yaml"
    if not source.is_file():
        raise FileNotFoundError(f"scene file not found: config/scenes/{name}.yaml")
    dest_dir.mkdir(parents=True, exist_ok=True)
    scene_dest = dest_dir / "scene.yaml"
    data_dest = dest_dir / "data_source.md"
    shutil.copyfile(source, scene_dest)
    shutil.copyfile(DATA_SOURCE, data_dest)
    return scene_dest, data_dest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Copy one scene yaml and data_source.md into the workspace")
    parser.add_argument("--scene-name", required=True, help="Stem of config/scenes/<scene_name>.yaml")
    parser.add_argument("--dest", type=Path, default=Path("."), help="Workspace directory")
    args = parser.parse_args(argv)
    try:
        scene_dest, data_dest = materialize(args.scene_name, args.dest)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise SystemExit(f"materialize_scene failed: {exc}") from exc
    print(scene_dest.resolve())
    print(data_dest.resolve())


if __name__ == "__main__":
    main(sys.argv[1:])
