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
from types import SimpleNamespace

from dataagent.core.context.context import ContextFactory
from dataagent.utils.info_utils import get_current_query


def test_get_current_query_uses_parent_user_query_from_subagent_runtime() -> None:
    runtime = SimpleNamespace(
        user_id="main-user",
        session_id="subagent_main-session_7",
        run_id=0,
        sub_id=7,
        parent_user_query="主 Agent 的原始问题",
    )

    assert get_current_query(runtime) == "主 Agent 的原始问题"


def test_get_current_query_keeps_same_process_context_fallback() -> None:
    ContextFactory.clear_context()
    context = ContextFactory.get_context(user_id="main-user", session_id="main-session", run_id=0, sub_id=0)
    context.register_query(query="同进程 Context 问题", additional_files=[])
    runtime = SimpleNamespace(
        user_id="main-user",
        session_id="main-session",
        run_id=0,
        sub_id=0,
        parent_user_query="",
    )

    try:
        assert get_current_query(runtime) == "同进程 Context 问题"
    finally:
        ContextFactory.clear_context()
