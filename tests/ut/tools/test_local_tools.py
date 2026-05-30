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
import pytest

from dataagent.actions.tools import BaseTool, ToolResult
from dataagent.core.managers.action_manager import ToolSchema
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.core.managers.action_manager.schemas import ParameterSchema

tool_manager = ToolManager()


def add_numbers(a: int, b: int) -> int:
    """
    计算两个数的和

    Args:
        a: 第一个数
        b: 第二个数

    Returns:
        两数之和
    """
    return a + b


@pytest.mark.asyncio
async def test_local_tool_register_by_sdk():
    tool_manager.register_local_tool(add_numbers, name="add_numbers", category="test")
    assert tool_manager.exists("add_numbers")
    assert tool_manager.get("add_numbers").name == "add_numbers"
    assert tool_manager.get("add_numbers").category == "test"
    assert tool_manager.get("add_numbers").func == add_numbers

    tool_schema = tool_manager.get("add_numbers").get_schema().to_dict()
    assert tool_schema.get("name") == "add_numbers"
    assert "计算两个数的和" in tool_schema.get("description")

    parameters = tool_schema.get("parameters")
    assert parameters[0].get("name") == "a"
    assert parameters[1].get("name") == "b"

    assert tool_schema.get("tool_type") == "local_function"

    result = tool_manager.call("add_numbers", a=1, b=2)
    assert result.success is True
    assert result.data == 3
    await tool_manager.cleanup()


@pytest.mark.asyncio
async def test_local_tool_register_by_class():
    class AddNumbers(BaseTool):
        def __init__(self, name: str, category: str, description: str, **kwargs):
            super().__init__(name, category, description, **kwargs)

        def get_schema(self) -> "ToolSchema":
            return ToolSchema(
                "add_numbers", "计算两个数的和", [ParameterSchema("a", "int"), ParameterSchema("b", "int")]
            )

        def call(self, **kwargs):
            return ToolResult(success=True, data=kwargs["a"] + kwargs["b"])

    tool_manager.register_local_tool(AddNumbers, name="add_numbers", category="test")
    assert tool_manager.exists("add_numbers")
    assert tool_manager.get("add_numbers").name == "add_numbers"
    assert tool_manager.get("add_numbers").category == "test"

    tool_schema = tool_manager.get("add_numbers").get_schema().to_dict()
    assert tool_schema.get("name") == "add_numbers"
    assert "计算两个数的和" in tool_schema.get("description")

    parameters = tool_schema.get("parameters")
    assert parameters[0].get("name") == "a"
    assert parameters[1].get("name") == "b"

    assert tool_schema.get("tool_type") == "custom"
    result = tool_manager.call("add_numbers", a=1, b=2)
    assert result.success is True
    assert result.data == 3
    await tool_manager.cleanup()


@pytest.mark.asyncio
async def test_register_local_tools_ignores_yaml_description_for_non_sub_agent_tools():
    """YAML ``description`` is ignored for tools other than ``sub_agent_tool``."""
    tm = ToolManager()
    tm._register_local_tools(
        [
            {
                "module": "tests.ut.tools.test_local_tools",
                "function": "add_numbers",
                "description": "YAML override description",
            }
        ]
    )
    desc = tm.get("add_numbers").description
    assert "计算两个数的和" in desc
    assert "YAML override description" not in desc
    await tm.cleanup()


@pytest.mark.asyncio
async def test_register_local_tools_falls_back_to_docstring_without_yaml_description():
    """Without a YAML ``description`` key, registration uses the function docstring."""
    tm = ToolManager()
    tm._register_local_tools(
        [
            {
                "module": "tests.ut.tools.test_local_tools",
                "function": "add_numbers",
            }
        ]
    )
    assert "计算两个数的和" in tm.get("add_numbers").description
    await tm.cleanup()


@pytest.mark.asyncio
async def test_register_local_tools_appends_yaml_description_for_sub_agent_tool():
    """``sub_agent_tool`` YAML description supplements the docstring instead of replacing it."""
    tm = ToolManager()
    yaml_supplement = "可选 config_path：/path/to/arithmetic.yaml"
    tm._register_local_tools(
        [
            {
                "module": "dataagent.actions.tools.local_tool.tools",
                "function": "sub_agent_tool",
                "description": yaml_supplement,
            }
        ]
    )
    desc = tm.get("sub_agent_tool").description
    assert "Starts a sub Agent in a separate subprocess" in desc
    assert yaml_supplement in desc
    assert "Supplement (from agent configuration)" in desc
    assert "Args:" in desc
    assert desc.index("Supplement (from agent configuration)") < desc.index("Args:")
    await tm.cleanup()
