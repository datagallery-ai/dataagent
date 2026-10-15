from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "dataagent"
    / "core"
    / "suite"
    / "builtin_suites"
    / "data_analysis"
    / "skill"
    / "user_sampling"
    / "scripts"
    / "materialize_scene.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("materialize_scene", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_materialize_copies_named_scene(tmp_path: Path) -> None:
    mod = _load_module()
    scene_dest, data_dest = mod.materialize("game_scene", tmp_path)
    text = scene_dest.read_text(encoding="utf-8")
    assert "scene_id: game_user_targeting" in text
    assert "match_terms:" in text
    assert "sql_warehouse" in data_dest.read_text(encoding="utf-8")


def test_materialize_rejects_unknown_or_unsafe_name(tmp_path: Path) -> None:
    mod = _load_module()
    with pytest.raises(FileNotFoundError):
        mod.materialize("missing_scene", tmp_path)
    with pytest.raises(ValueError):
        mod.materialize("../scene", tmp_path)
