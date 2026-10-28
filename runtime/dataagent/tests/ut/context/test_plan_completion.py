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
"""The reply after the last todo is completed is the answer the user receives."""

from pathlib import Path

import pytest

from dataagent.core.context.context import ContextFactory, ContextInitOptions
from dataagent.core.context.todolist_manager import TodoListManager
from dataagent.core.flex.utils.planner_prompt_builder import build_todo_message


@pytest.fixture(autouse=True)
def isolated_contexts():
    """Release Context instances around each test to simulate independent requests."""
    ContextFactory.clear_context()
    yield
    ContextFactory.clear_context()


def _context(workspace: Path, run_id: int):
    return ContextFactory.get_context("u", "s", run_id, 0, options=ContextInitOptions(workspace=workspace))


def test_completing_last_todo_asks_for_full_final_reply() -> None:
    manager = TodoListManager()
    manager.create_plan(introduction="整理数据", approach="分步执行", todos=["读取数据", "输出结果"])

    assert manager.complete_current_todo() == "Todo '读取数据' completed successfully."
    last = manager.complete_current_todo()
    assert last.startswith("Todo '输出结果' completed successfully. All todos in the plan are now completed.")
    assert "Your next reply is the final answer the user receives" in last
    assert "in full" in last
    assert manager.complete_current_todo() == "No incomplete todos found. All todos are already completed."


def test_in_progress_prompt_keeps_completion_message_brief(tmp_path: Path) -> None:
    context = _context(tmp_path, 0)
    context.todolist_manager.create_plan(introduction="整理数据", approach="分步执行", todos=["读取数据", "输出结果"])

    prompt = str(build_todo_message(context).content)

    assert "**Current todo (execute this step now):** 读取数据" in prompt
    assert "When calling **`complete_current_todo`**, keep the accompanying message to a brief step status" in prompt


def test_completed_plan_prompt_asks_for_full_answer_instead_of_summary(tmp_path: Path) -> None:
    first = _context(tmp_path, 0)
    first.todolist_manager.create_plan(introduction="整理数据", approach="分步执行", todos=["读取数据"])
    first.todolist_manager.complete_current_todo()
    ContextFactory.clear_context()

    # A completed plan is restored for the next request of the session, so the
    # final-answer instruction must only apply to the request the plan was made for.
    prompt = str(build_todo_message(_context(tmp_path, 1)).content)

    assert "All todos in the current plan are **completed**" in prompt
    assert "**Summarize** what was accomplished" not in prompt
    final_answer = next(line for line in prompt.splitlines() if "**final answer the user receives**" in line)
    assert final_answer.startswith("- If this plan was made for the user's latest request")
    assert "in full" in final_answer
    assert "**`create_plan`**" in prompt
    assert "**`delete_plan`**" in prompt
