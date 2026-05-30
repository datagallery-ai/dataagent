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
from __future__ import annotations

# NOTE: The following imports will resolve once galatea agent code is merged in
# (dataagent/agents/galatea/agent.py and dataagent/agents/galatea/state/state.py).
# Until then, this tool cannot be loaded by ActionManager.register_tool().
from dataagent.agents.galatea.agent import Galatea
from dataagent.agents.galatea.state.state import State
from dataagent.core.cbb.agent_env import Env


def create_subagent(
    template: str,
    user_query: str,
    user_id: str,
    env: Env,
    hierarchy: str = "SUB",
) -> str:
    """
    Delegate a task to a subagent. The subagent runs with a fresh
    state and will work on the delegated task, then return its final response.

    Delegation guidance: Prefer subtasks that are neither too atomized nor the
    whole task. Avoid delegating trivial one-step work; avoid delegating the
    entire task unless it is simple enough. Choose subtasks that are
    meaningful, self-contained chunks of work.

    Args:
        template: The template to use for the subagent. Currently supporting:
            - default
        user_query: The task to delegate. Describe what to do and point the
            subagent to any artifacts it should read or use, e.g., file paths,
            relevant context, or outputs from prior steps.

    Returns:
        The subagent's final response.
    """
    subagent = Galatea(name="Mnemosyne", env=env)

    sub_state = State(
        enable_hierarchical_orchestration=False,
        enable_portrait=False,
        hierarchy=hierarchy,
        user_id=user_id,
        session_id="",
        instructions="",
        user_query=user_query,
        curr_iter=0,
        messages=[],
    )

    final_state = subagent.invoke(sub_state, env)

    try:
        response = final_state["messages"][-1].content
    except Exception:
        response = "Subagent completed with no response."

    return response
