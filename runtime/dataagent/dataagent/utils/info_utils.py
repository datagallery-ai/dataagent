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
from typing import Optional

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.context.context import ContextFactory


def get_current_query(runtime: Runtime) -> Optional[str]:  # noqa: UP045
    """获取当前工具调用上下文中的原始用户查询，注意：即使在子 agent 中调用，也会返回用户原始 query（即主 Agent 的 query）。"""
    user_id = runtime.user_id
    session_id = runtime.session_id
    run_id = runtime.run_id
    sub_id = 0  # 锁定使用主 Agent 的 query

    parent_query = getattr(runtime, "parent_user_query", None)
    if isinstance(parent_query, str) and parent_query:
        return parent_query

    context = ContextFactory.get_context(user_id, session_id, run_id, sub_id)
    query = _get_query_from_context(context)
    if query is not None:
        return query

    return None


def _get_query_from_context(context) -> Optional[str]:  # noqa: UP045
    if not context.has_initial_pt:
        return None
    trajectory = context.get_trajectory(trimmed=False)
    query_node = trajectory.nodes.get(context.initial_pt, {})
    query = query_node.get("query") if isinstance(query_node, dict) else None
    return query if isinstance(query, str) else None
