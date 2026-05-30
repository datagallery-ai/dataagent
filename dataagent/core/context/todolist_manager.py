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
    """
    A node in the todo list.
    """

    name: str
    params: dict[str, Any] = field(default_factory=dict)


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
        self.todolist = deque(maxlen=maxlen)
        self.postlist = deque(maxlen=maxlen)
        self.lists = {
            "pre": self.prelist,
            "todo": self.todolist,
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

    def add_node(self, node: TodoNode | dict, list_type: str = "todo") -> bool:
        """
        Add a node to the specified list.

        Args:
            node (TodoNode | dict): The node to add.
            list_type (str): The list to add the node to. One of "pre", "todo", "post".

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

    def pop_node(self, list_type: str = "todo") -> TodoNode | None:
        """
        Pop a node from the specified list.

        Args:
            list_type (str): The list to pop the node from. One of "pre", "todo", "post".

        Returns:
            TodoNode | None, the popped node, or None if the list is empty.
        """
        if list_type not in self.lists:
            raise ValueError(f"TodoListManager: Invalid list type: {list_type}")
        if not self.lists[list_type]:
            return None
        return self.lists[list_type].popleft()
