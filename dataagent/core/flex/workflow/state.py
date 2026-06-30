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
from operator import add
from pathlib import Path
from typing import Annotated

from dataagent.core.cbb.base_state import BaseState


class FlexState(BaseState):
    complete: bool
    num_turns: Annotated[int, add]
    num_valid_tool_calls: Annotated[int, add]
    num_invalid_tool_calls: Annotated[int, add]
    user_id: str
    session_id: str
    run_id: int
    sub_id: int
    workspace: Path
    enable_portrait: bool = False  # LLM 用户画像 / snapshot·profile；与 messages.json 持久化无关

    # Human-in-the-Loop (HITL) fields
    enable_human_feedback: bool = False  # Global switch for HITL
    need_human_feedback: bool = False  # Flag set by Actor, read by Router
    feedback: str = ""  # Accumulated user feedback
    terminal_mode: bool = False  # Terminal mode for local debugging
    hitl_count: Annotated[int, add] = 0  # Number of HITL rounds (累加器)

    # Internal control fields
    __hitl_in_current_turn__: bool = False  # Prevent re-entry in same turn
    __human_feedback_resume__: str = ""  # OpenJiuWen resume mechanism

    # Cross-session memory: Retrieved historical session summaries for context
    cross_session_memory: str = ""

    # Intent understanding (意图理解模板)
    intent_complete: bool = True  # 本轮意图是否填满，默认 True 保证向后兼容
    intent_slots: dict = {}  # 已抽取的槽位 {field: value}
    missing_slots: list = []  # 缺口 [{field, reason, impact}]
    intent_missing_message: str = ""  # 槽位缺失时返回给用户的前端提示
