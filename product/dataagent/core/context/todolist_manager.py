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
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class TodoNode:
    """单条待办节点，包含工具名称和参数。"""

    name: str  # 工具名称
    params: dict[str, Any] = field(default_factory=dict)  # 工具参数


@dataclass
class TodoItem:
    """单条待办信息，包含标题和完成状态。"""

    title: str  # 待办标题
    completed: bool = False  # 是否完成


@dataclass
class Plan:
    """当前内存中的计划快照，包含计划介绍、开发思路和待办列表。"""

    introduction: str  # plan 介绍
    approach: str  # 开发思路
    todos: list[TodoItem] = field(default_factory=list)  # 待办列表


class TodoListManager:
    """
    Manages three lists of todo nodes: pre, todo, and post.
    """

    def __init__(
        self,
        maxlen: int = 100,
        *,
        pre_workflow: Sequence[dict[str, Any]] | None = None,
        post_workflow: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        """
        Initialize TodoListManager.

        Args:
            maxlen (int): The maximum number of nodes each internal deque can hold.
                When the deque reaches this size, further additions to that deque will fail.
            pre_workflow: PRE_WORKFLOW node definitions from Agent YAML.
            post_workflow: POST_WORKFLOW node definitions from Agent YAML.
        """
        self._pre_workflow: tuple[dict[str, Any], ...] = tuple(pre_workflow or ())
        self._post_workflow: tuple[dict[str, Any], ...] = tuple(post_workflow or ())
        self.prelist = deque(maxlen=maxlen)
        self.todolist: Plan | None = None
        self.postlist = deque(maxlen=maxlen)
        self.lists = {
            "pre": self.prelist,
            "post": self.postlist,
        }

    def init_todolist(self) -> None:
        """
        Initialize the todo lists from configured PRE/POST workflow definitions.
        """
        if not self._pre_workflow and not self._post_workflow:
            raise RuntimeError("TodoListManager.init_todolist requires pre_workflow or post_workflow definitions.")
        self.prelist.clear()
        self.postlist.clear()
        for item in self._pre_workflow:
            self.add_node(TodoNode(name=item["name"], params=item.get("params", {})), list_type="pre")
        for item in self._post_workflow:
            self.add_node(TodoNode(name=item["name"], params=item.get("params", {})), list_type="post")

    def add_node(self, node: TodoNode | dict, list_type: str = "pre") -> bool:
        """
        Add a node to the specified list.

        Args:
            node (TodoNode | dict): The node to add.
            list_type (str): The list to add the node to. One of "pre", "post".

        Returns:
            bool, True if the node was added, False otherwise.
        """
        if isinstance(node, dict):
            try:
                node = TodoNode(name=node["name"], params=node.get("params", {}))
            except TypeError as e:
                raise ValueError(f"TodoListManager: Failed to create TodoNode from dict: {node}, error: {e}") from e
        if list_type not in self.lists:
            raise ValueError(f"TodoListManager: Invalid list type: {list_type}")
        if len(self.lists[list_type]) >= self.lists[list_type].maxlen:  # type: ignore
            logger.warning(f"TodoListManager: {list_type} list is full, cannot add node: {node}")
            return False
        self.lists[list_type].append(node)
        return True

    def pop_node(self, list_type: str = "pre") -> TodoNode | None:
        """
        Pop a node from the specified list.

        Args:
            list_type (str): The list to pop the node from. One of "pre", "post".

        Returns:
            TodoNode | None, the popped node, or None if the list is empty.
        """
        if list_type not in self.lists:
            raise ValueError(f"TodoListManager: Invalid list type: {list_type}")
        if not self.lists[list_type]:
            return None
        return self.lists[list_type].popleft()

    def create_plan(self, *, introduction: str, approach: str, todos: list[str]) -> None:
        """
        Create or replace the in-memory global plan for the current process.

        The new plan overwrites any existing plan. All todo items start as incomplete.
        State is guarded by a lock and the returned ``Plan`` is a deep copy of the
        stored snapshot.

        Args:
            introduction (str): High-level description of what the plan covers.
            approach (str): Development strategy or implementation approach.
            todos (list[str]): Ordered todo titles; each becomes an incomplete ``TodoItem``.
        """
        self.todolist = Plan(
            introduction=introduction, approach=approach, todos=[TodoItem(title=t, completed=False) for t in todos]
        )

    def update_plan(
        self,
        *,
        introduction: str | None = None,
        approach: str | None = None,
        todos: list[str] | None = None,
    ) -> None:
        """
        Apply field-level updates to the current in-memory global plan.

        Only arguments that are not ``None`` are applied. When ``todos`` is provided,
        the todo list is replaced in full (titles only; all new items are incomplete).

        Args:
            introduction (str | None): New introduction text; omit to keep the current value.
            approach (str | None): New approach text; omit to keep the current value.
            todos (list[str] | None): If set, replaces the entire todo list with new items.
        """
        if self.todolist is None:
            logger.warning("TodoListManager: No plan found. You must create a plan first.")
            return

        new_intro = introduction if introduction is not None else self.todolist.introduction
        new_approach = approach if approach is not None else self.todolist.approach
        if todos is not None:
            new_todos = [TodoItem(title=t, completed=False) for t in todos]
        else:
            new_todos = list(self.todolist.todos)

        self.todolist = Plan(introduction=new_intro, approach=new_approach, todos=new_todos)

    def delete_plan(self) -> None:
        """
        Remove the in-memory global plan for the current process. Subsequent reads behave as if no
        plan was ever created until ``create_plan`` is called again.
        """
        self.todolist = None

    def complete_current_todo(self) -> str:
        """
        Mark the first incomplete todo in the global plan as completed.

        "Current" todo is defined as the first ``TodoItem`` with ``completed=False`` in list order.

        Returns:
            str: status of todo completion.
        """
        if self.todolist is None:
            return "No plan found. You must create a plan first."

        for todo in self.todolist.todos:
            if not todo.completed:
                todo.completed = True
                return f"Todo '{todo.title}' completed successfully."

        return "No incomplete todos found. All todos are already completed."
