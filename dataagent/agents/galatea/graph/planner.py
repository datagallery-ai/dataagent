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
import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from langchain_core.messages import HumanMessage, SystemMessage

from dataagent.agents.galatea.state.state import State
from dataagent.agents.galatea.utils.portraiter_utils import load_user_profile, load_user_snapshot
from dataagent.core.cbb.base_node import BaseNode
from dataagent.core.cbb.runtime import Runtime

# Single IANA zone for all planner prompt timestamps (override via GALATEA_TIMEZONE).
_DEFAULT_PLANNER_TZ = "UTC"
_ENV_PLANNER_TZ = "GALATEA_TIMEZONE"


def _planner_zone() -> ZoneInfo:
    name = (os.environ.get(_ENV_PLANNER_TZ) or _DEFAULT_PLANNER_TZ).strip() or _DEFAULT_PLANNER_TZ
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(_DEFAULT_PLANNER_TZ)


class PlannerNode(BaseNode):
    def __init__(self):
        super().__init__(name="planner", pre_hooks=[], post_hooks=[])

    @staticmethod
    def _prepare_system_prompt(state: State, project_root: Path) -> str:
        system_template_path = project_root / "agents" / "galatea" / "prompts" / "planner" / "system.md"
        system_prompt = system_template_path.read_text(encoding="utf-8")
        instructions = str(state.get("instructions", "") or "").strip()
        if not instructions:
            return system_prompt
        return (
            f"{system_prompt}\n\n"
            "Mandatory instructions for solving the user query (MUST FOLLOW):\n"
            "- You MUST strictly follow all instructions below.\n"
            "- You MUST NOT ignore or weaken these instructions.\n"
            "- If any instruction below conflicts with your default strategy, prioritize the instructions below.\n"
            f"{instructions}"
        )

    @staticmethod
    def _prepare_user_prompt(user_prompt_variables: dict, project_root: Path) -> str:
        user_template_path = project_root / "agents" / "galatea" / "prompts" / "planner" / "user.md"
        user_template = Template(user_template_path.read_text(encoding="utf-8"))
        return user_template.safe_substitute(**user_prompt_variables)

    def _process(self, state: State, runtime: Runtime) -> State:
        runtime.ensure_not_cancelled()
        updated_state = deepcopy(state)
        project_root = Path(__file__).parent.parent.parent.parent

        messages = state["messages"]
        if state.get("curr_iter", 0) == 0:
            user_prompt_variables = self._prepare_user_prompt_variables(state, runtime)
            system_prompt = self._prepare_system_prompt(state, project_root)
            user_prompt = self._prepare_user_prompt(user_prompt_variables, project_root)
            updated_state["messages"] = [SystemMessage(system_prompt), *messages, HumanMessage(user_prompt)]

        updated_state["_stream_emitted_from"] = len(updated_state["messages"])

        action_manager = self.get_module("action_manager")
        if state["enable_hierarchical_orchestration"] and state["hierarchy"].upper() == "MAIN":
            try:
                tools = [action_manager.get_tool("create_subagent")]
            except ValueError as e:
                raise ValueError("Register create_subagent tool before enabling hierarchical orchestration.") from e
        else:
            tools = action_manager.get_tools()

        llm = runtime.llm("planner").bind_tools(tools)
        response = llm.invoke(updated_state["messages"])

        updated_state["messages"].append(response)

        return updated_state

    def _prepare_user_prompt_variables(self, state: State, runtime: Runtime) -> dict:
        user_query = state["user_query"]
        try:
            geo = requests.get("https://ipinfo.io/json", timeout=10).json()
        except (requests.RequestException, json.JSONDecodeError):
            geo = {}
        zone = _planner_zone()
        now = datetime.now(zone)
        user_prompt_variables = {
            "user_query": user_query,
            "date": now.strftime("%Y-%m-%d"),
            "location": geo.get("city", "Unknown"),
            "local_time": now.strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)"),
            "memory": "",
        }

        workspace_dir = Path(getattr(runtime.env, "workspace_dir", Path.cwd()))
        user_snapshot = load_user_snapshot(state["user_id"], workspace_dir)
        user_profile = load_user_profile(state["user_id"], workspace_dir)
        memory = (
            "User Snapshot:\n"
            f"{json.dumps(user_snapshot, ensure_ascii=False, indent=2)}\n\n"
            "User Profile:\n"
            f"{json.dumps(user_profile, ensure_ascii=False, indent=2)}"
        )
        user_prompt_variables["memory"] = memory

        return user_prompt_variables
