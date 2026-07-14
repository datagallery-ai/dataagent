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

from dataagent.core.context.context import _resolve_show_output_path


def test_show_output_path_stays_inside_workspace(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    output = _resolve_show_output_path(
        output_html="reports/trajectory.html",
        user_id="u",
        session_id="s",
        run_id=1,
        sub_id=0,
        workspace=str(workspace),
        config=None,
    )

    assert output == workspace / "reports" / "trajectory.html"


def test_show_output_path_rejects_workspace_escape(tmp_path: Path) -> None:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    with pytest.raises(ValueError, match="output_html"):
        _resolve_show_output_path(
            output_html="../outside.html",
            user_id="u",
            session_id="s",
            run_id=1,
            sub_id=0,
            workspace=str(workspace),
            config=None,
        )

    with pytest.raises(ValueError, match="output_html"):
        _resolve_show_output_path(
            output_html=str(tmp_path / "outside.html"),
            user_id="u",
            session_id="s",
            run_id=1,
            sub_id=0,
            workspace=str(workspace),
            config=None,
        )
