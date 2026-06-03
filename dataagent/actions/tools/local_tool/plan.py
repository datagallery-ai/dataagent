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
from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.core.context.context_trajectory import Context, ContextFactory


def create_plan(
    introduction: str,
    approach: str,
    todos: list[str],
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, str]:
    """
    Create or replace the in-memory global plan for the current process.

    The new plan overwrites any existing plan. All todo items start as incomplete.
    State is guarded by a lock and the returned ``Plan`` is a deep copy of the
    stored snapshot.

    Args:
        introduction (str): High-level description of what the plan covers.
        approach (str): Development strategy or implementation approach.
        todos (list[str]): Ordered todo titles; each becomes an incomplete ``TodoItem``.

    Returns:
        dict[str, str]: Dictionary containing the original message and frontend message.
    """
    try:
        context = _get_context(_tool_context)
    except ValueError as e:
        return {
            "original_msg": str(e),
            "frontend_msg": str(e),
        }

    context.todolist_manager.create_plan(introduction=introduction, approach=approach, todos=todos)
    return {
        "original_msg": "Plan created successfully.",
        "frontend_msg": f"Plan created successfully with {len(todos)} todos.\n\n"
        + "\n".join([f"- {t}" for t in todos]),
    }


def update_plan(
    introduction: str | None = None,
    approach: str | None = None,
    todos: list[str] | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, str]:
    """
    Apply field-level updates to the current in-memory global plan.

    Only arguments that are not ``None`` are applied. When ``todos`` is provided,
    the todo list is replaced in full (titles only; all new items are incomplete).

    Args:
        introduction (str | None): New introduction text; omit to keep the current value.
        approach (str | None): New approach text; omit to keep the current value.
        todos (list[str] | None): If set, replaces the entire todo list with new items.

    Returns:
        dict[str, str] | None: Dictionary containing the original message and frontend message.
    """
    try:
        context = _get_context(_tool_context)
    except ValueError as e:
        return {
            "original_msg": str(e),
            "frontend_msg": str(e),
        }

    if context.todolist_manager.todolist is None:
        return {
            "original_msg": "No plan found. You must create a plan first.",
            "frontend_msg": "No plan found.",
        }

    context.todolist_manager.update_plan(introduction=introduction, approach=approach, todos=todos)
    new_todos = (
        context.todolist_manager.todolist.todos if todos is not None else context.todolist_manager.todolist.todos
    )
    return {
        "original_msg": "Plan updated successfully.",
        "frontend_msg": f"Plan updated successfully with {len(new_todos)} todos.\n\n"
        + "\n".join([f"- {t.title}" for t in new_todos]),
    }


def delete_plan(*, _tool_context: ToolExecutionContext) -> dict[str, str]:
    """
    Remove the in-memory global plan for the current process.

    Subsequent reads behave as if no plan was ever created until ``create_plan`` is called again.

    Returns:
        dict[str, str]: Dictionary containing the original message and frontend message.
    """
    try:
        context = _get_context(_tool_context)
    except ValueError as e:
        return {
            "original_msg": str(e),
            "frontend_msg": str(e),
        }

    if context.todolist_manager.todolist is None:
        return {
            "original_msg": "No plan found. You must create a plan first.",
            "frontend_msg": "No plan found.",
        }

    context.todolist_manager.delete_plan()
    return {
        "original_msg": "Plan deleted successfully.",
        "frontend_msg": "Plan deleted successfully.",
    }


def complete_current_todo(*, _tool_context: ToolExecutionContext) -> dict[str, str]:
    """
    Mark the first incomplete todo in the global plan as completed.

    "Current" todo is defined as the first ``TodoItem`` with ``completed=False`` in list order.

    Returns:
        dict[str, str]: Dictionary containing the original message and frontend message.
    """
    try:
        context = _get_context(_tool_context)
    except ValueError as e:
        return {
            "original_msg": str(e),
            "frontend_msg": str(e),
        }

    result = context.todolist_manager.complete_current_todo()
    return {
        "original_msg": result,
        "frontend_msg": result,
    }


def _get_context(_tool_context: ToolExecutionContext) -> Context:
    """Get the context for the current tool call."""
    if _tool_context.runtime is None:
        raise ValueError("Tool Runtime is not found. Please proceed without a plan.")

    context_args = {
        "user_id": _tool_context.runtime.user_id,
        "session_id": _tool_context.runtime.session_id,
        "run_id": _tool_context.runtime.run_id,
        "sub_id": _tool_context.runtime.sub_id,
    }
    return ContextFactory.get_context(**context_args)
