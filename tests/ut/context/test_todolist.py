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
from pathlib import Path
from typing import cast

from dataagent.config.config_manager import ConfigManager
from dataagent.core.context.context_trajectory import ContextFactory, build_context_init_options


class TestTodoList:
    """Tests for Context todo-list operations."""

    _config_manager: ConfigManager

    def setup_class(self):
        """Initialize Context object"""
        PROJECT_DIR = Path(__file__).resolve().parents[2]
        config_path = PROJECT_DIR / "ut" / "context" / "ut_data" / "test_todolist.yaml"
        TestTodoList._config_manager = ConfigManager()
        TestTodoList._config_manager.reload(str(config_path))
        self.context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer",
            session_id="#00001",
            run_id=0,
            sub_id=0,
            options=build_context_init_options(TestTodoList._config_manager),
        )

    def test_context_initialized(self):
        assert self.context is not None
        assert self.context._user_id == "jiutian_applicationlayer"
        assert self.context._session_id == "#00001"
        assert self.context._run_id == 0
        assert self.context._sub_id == 0

    def test_init_todolist_manager(self):
        self.context._todolist_manager.init_todolist()
        assert self.context._todolist_manager is not None
        assert len(self.context._todolist_manager.prelist) == 1
        assert self.context._todolist_manager.todolist is None
        assert len(self.context._todolist_manager.postlist) == 2

    def test_append_and_pop_todo(self):
        """Test adding and popping todo nodes"""
        # Add todo nodes
        maxlen = cast(int, self.context._todolist_manager.prelist.maxlen) - 1
        for i in range(maxlen):
            added = self.context.append_todo(name=f"todo_node_{i}", params={"param1": f"value_{i}"}, list_type="pre")
            assert added is True
        # Try to add one more node beyond maxlen
        added = self.context.append_todo(name="extra_todo_node", params={"param1": "extra_value"}, list_type="pre")
        assert added is False

        # Test adding to an unknown list type raises an exception
        try:
            self.context.append_todo(name="invalid_node", params={}, list_type="invalid_list")
        except ValueError as e:
            assert "Invalid list type" in str(e)

        # Pop todo nodes
        post_list = TestTodoList._config_manager.get("POST_WORKFLOW", [])
        for i in range(len(post_list)):
            node = self.context.pop_todo(list_type="post")
            assert node is not None
            assert node["name"] == post_list[i]["name"]
            assert node["params"] == post_list[i].get("params", {})

        # pop empty post list
        node = self.context.pop_todo(list_type="post")
        assert node is None


if __name__ == "__main__":
    testTodoList = TestTodoList()
    testTodoList.setup_class()
    testTodoList.test_context_initialized()
    testTodoList.test_init_todolist_manager()
    testTodoList.test_append_and_pop_todo()
