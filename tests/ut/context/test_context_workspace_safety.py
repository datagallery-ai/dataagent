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
from pathlib import Path

import pytest

from dataagent.core.context.context import build_context_init_options


class _ConfigManager:
    def __init__(self, settings: dict):
        self._settings = settings

    def get(self, key: str, default=None):
        return self._settings.get(key, default)

    def get_all(self):
        return dict(self._settings)


def test_build_context_init_options_accepts_workspace_under_configured_root(tmp_path: Path) -> None:
    root = (tmp_path / "allowed").resolve()
    child = root / "child"
    settings = {
        "USER_ID": "u",
        "SESSION_ID": "s",
        "WORKSPACE": {"path": str(root)},
    }

    options = build_context_init_options(_ConfigManager(settings), workspace=child)

    assert options.workspace == child.resolve()


def test_build_context_init_options_rejects_workspace_outside_configured_root(tmp_path: Path) -> None:
    root = (tmp_path / "allowed").resolve()
    outside = (tmp_path / "outside").resolve()
    settings = {
        "USER_ID": "u",
        "SESSION_ID": "s",
        "WORKSPACE": {"path": str(root)},
    }

    with pytest.raises(ValueError, match="workspace"):
        build_context_init_options(_ConfigManager(settings), workspace=outside)
