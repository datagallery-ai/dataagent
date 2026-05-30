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
"""Runtime session identity (user_id / session_id / run_id / sub_id) tests."""

from pathlib import Path

from dataagent.core.cbb.agent_env import Env
from dataagent.core.cbb.runtime import Runtime


def _minimal_env(**kwargs) -> Env:
    """Build a minimal Env for unit tests."""
    return Env(
        llm_configs={},
        tavily_configs={},
        modules={},
        hooks={},
        **kwargs,
    )


class TestRuntimeSessionIdentity:
    """Verify Runtime defaults and update_from_state sync."""

    def test_default_session_identity(self):
        """New Runtime uses Flex-aligned default identifiers."""
        runtime = Runtime(_minimal_env())
        assert runtime.user_id == "anonymous"
        assert runtime.session_id == "default_session"
        assert runtime.run_id == 0
        assert runtime.sub_id == 0

    def test_update_from_state_syncs_identity_and_workspace(self, tmp_path: Path):
        """update_from_state copies session fields and workspace from state."""
        env = _minimal_env()
        runtime = Runtime(env)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        runtime.update_from_state(
            {
                "user_id": "user_a",
                "session_id": "sess_b",
                "run_id": 3,
                "sub_id": 2,
                "workspace": str(workspace),
            }
        )
        assert runtime.user_id == "user_a"
        assert runtime.session_id == "sess_b"
        assert runtime.run_id == 3
        assert runtime.sub_id == 2
        assert runtime.workspace_dir == workspace.resolve()

    def test_update_from_state_empty_strings_fall_back_to_defaults(self):
        """Blank user_id / session_id in state map to anonymous / default_session."""
        runtime = Runtime(_minimal_env())
        runtime.update_from_state(
            {
                "user_id": "  ",
                "session_id": "",
                "run_id": 1,
                "sub_id": 0,
            }
        )
        assert runtime.user_id == "anonymous"
        assert runtime.session_id == "default_session"
        assert runtime.run_id == 1
        assert runtime.sub_id == 0

    def test_partial_state_update_preserves_previous_identity(self):
        """When state omits keys, prior runtime identity values are kept."""
        runtime = Runtime(_minimal_env())
        runtime.update_from_state(
            {
                "user_id": "u1",
                "session_id": "s1",
                "run_id": 5,
                "sub_id": 1,
            }
        )
        runtime.update_from_state({"workspace": "/tmp"})
        assert runtime.user_id == "u1"
        assert runtime.session_id == "s1"
        assert runtime.run_id == 5
        assert runtime.sub_id == 1
