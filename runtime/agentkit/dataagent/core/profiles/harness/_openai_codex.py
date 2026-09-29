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
# 本文件中的模型可见文案常量原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""给三把 OpenAI Codex 模型键登记同一条 harness 画像。

三把键共用同一次 ``register`` 里构造的那一个画像对象。
额外中间件是工厂：每次取出都新建一个 ``TodoListMiddleware``。
导入时不登记。
"""

from dataagent.core.profiles.harness.harness_profiles import HarnessProfile as RuntimeProfile
from dataagent.core.profiles.harness.harness_profiles import (
    _register_harness_profile_impl as publish,
)
from langchain.agents.middleware import TodoListMiddleware

_CODEX_KEYS = ("openai:gpt-5.1-codex", "openai:gpt-5.2-codex", "openai:gpt-5.3-codex")

_CODEX_SUFFIX = """\
## Codex-Specific Behavior

- You are an autonomous senior engineer. Once given a direction, proactively gather context, plan, implement, and verify without waiting for additional prompts at each step.
- Persist until the task is fully handled end-to-end within the current turn whenever feasible. Do not stop at analysis or partial fixes; carry changes through implementation, verification, and a clear explanation of outcomes.
- Bias to action: default to implementing with reasonable assumptions. Do not end your turn with clarifications unless truly blocked.
- Do not communicate an upfront plan or status preamble before acting. Just act.

## Parallel Tool Use

- Before any tool call, decide ALL files and resources you will need.
- Batch reads, searches, and other independent operations into parallel tool calls instead of issuing them one at a time.
- Only make sequential calls when you truly cannot determine the next step without seeing a prior result.

## Plan Hygiene

- Before finishing, reconcile every TODO or plan item created via write_todos. Mark each as done, blocked (with a one-sentence reason), or cancelled. Do not finish with pending items."""


def _fresh_todo_middleware():
    """每个 agent 栈拿自己的待办中间件，不把同一个实例挂到多处。"""
    created = TodoListMiddleware()
    return [created]


def register() -> None:
    """按固定顺序把同一份 Codex 画像登记到三把键上。"""
    profile = RuntimeProfile(
        system_prompt_suffix=_CODEX_SUFFIX,
        extra_middleware=_fresh_todo_middleware,
    )
    for spec in _CODEX_KEYS:
        publish(spec, profile)
