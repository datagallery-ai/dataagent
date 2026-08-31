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
import yaml

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
async def test_implicit_sub_agent_tool_appends_catalog_description(tmp_path):
    """``SUBAGENT_CONFIGS`` implicitly registers job lifecycle tools with catalog text."""
    subagent_yaml = tmp_path / "worker.yaml"
    subagent_yaml.write_text(
        yaml.safe_dump({"AGENT_CONFIG": {"name": "arith", "description": "does math"}}),
        encoding="utf-8",
    )
    tm = ToolManager()
    tm._register_implicit_job_tools({"SUBAGENT_CONFIGS": [{"path": str(subagent_yaml)}]})
    desc = tm.get("submit_subagent").description
    assert "does math" in desc
    assert "agent_id" in desc
    assert tm.exists("poll_subagent")
    assert not tm.exists("sub_agent_tool")
    await tm.cleanup()


@pytest.mark.asyncio
async def test_register_local_tools_rejects_explicit_sub_agent_tool():
    """Explicit ``sub_agent_tool`` in ``TOOLS.local_functions`` is forbidden."""
    tm = ToolManager()
    with pytest.raises(ValueError, match="SUBAGENT_CONFIGS"):
        tm._register_local_tools(
            [
                {
                    "module": "dataagent.actions.tools.local_tool.tools",
                    "function": "sub_agent_tool",
                }
            ]
        )
    await tm.cleanup()


def _func_with_docstring_args(path: str, purpose: str, offset: int | None = 1, limit: int | None = None) -> dict:
    """Read a file from the local filesystem.

    Reads a file and returns its content. You can access any file directly by
    using this tool. If the user provides a path to a file, assume that path is
    valid. It is okay to read a file that does not exist; an error will be
    returned.

    Usage:
    - Use absolute paths under the workspace root for workspace files. For
      additional read-only roots listed in the task context, use their absolute
      host paths. For skill resources, use ``skill/<name>/...`` paths.
    - By default it reads the entire file from line 1. For large files, use
      ``offset`` and ``limit`` to read only the relevant section.
    - When you already know which part of the file you need, only read that
      part — this is important for larger files.
    - Results are returned with line numbers (``N\\tline``) starting at 1.
    - This tool can only read text files, not directories. To list a directory,
      use the bash tool with ``ls``.
    - If you read a file that exists but has empty contents you will receive a
      system reminder warning in place of file contents.
    - Binary files and files exceeding the size budget will be rejected with a
      clear error message.

    Args:
        path (str): Absolute path under the workspace root, read-only root, or
            ``skill/<name>/...`` for skill assets.
        purpose (str): Brief description of why this file is being read (required, non-empty).
        offset (int | None): The line number to start reading from (1-based). Only provide
            if the file is too large to read at once.
        limit (int | None): The number of lines to read. Only provide if the file is too
            large to read at once.
    """
    return {}


@pytest.mark.asyncio
async def test_from_function_parses_docstring_param_descriptions():
    """ToolSchema.from_function extracts parameter descriptions from docstring Args section."""
    tm = ToolManager()
    tm.register_local_tool(_func_with_docstring_args, name="func_with_docstring_args", category="test")

    schema = tm.get("func_with_docstring_args").get_schema()

    assert (
        schema.description
        == "Read a file from the local filesystem.\nReads a file and returns its content. You can access any file directly by\nusing this tool. If the user provides a path to a file, assume that path is\nvalid. It is okay to read a file that does not exist; an error will be\nreturned.\nUsage:\n- Use absolute paths under the workspace root for workspace files. For\nadditional read-only roots listed in the task context, use their absolute\nhost paths. For skill resources, use ``skill/<name>/...`` paths.\n- By default it reads the entire file from line 1. For large files, use\n``offset`` and ``limit`` to read only the relevant section.\n- When you already know which part of the file you need, only read that\npart — this is important for larger files.\n- Results are returned with line numbers (``N\\tline``) starting at 1.\n- This tool can only read text files, not directories. To list a directory,\nuse the bash tool with ``ls``.\n- If you read a file that exists but has empty contents you will receive a\nsystem reminder warning in place of file contents.\n- Binary files and files exceeding the size budget will be rejected with a\nclear error message."
    )

    param_dict = {p.name: p for p in schema.parameters}

    assert (
        param_dict["path"].description == "Absolute path under the workspace root, read-only root, or "
        "``skill/<name>/...`` for skill assets."
    )
    assert (
        param_dict["purpose"].description == "Brief description of why this file is being read (required, non-empty)."
    )
    assert (
        param_dict["offset"].description
        == "The line number to start reading from (1-based). Only provide if the file is too large to read at once."
    )
    assert (
        param_dict["limit"].description
        == "The number of lines to read. Only provide if the file is too large to read at once."
    )

    assert param_dict["path"].required is True
    assert param_dict["purpose"].required is True
    assert param_dict["offset"].required is False
    assert param_dict["limit"].required is False

    await tm.cleanup()


def _func_no_docstring(path: str, name: str) -> None:
    return None


@pytest.mark.asyncio
async def test_from_function_falls_back_to_placeholder_when_no_docstring():
    """When docstring has no Args section, parameter description falls back to 'Parameter {name}'."""
    tm = ToolManager()
    tm.register_local_tool(_func_no_docstring, name="func_no_docstring", category="test")

    schema = tm.get("func_no_docstring").get_schema()
    param_dict = {p.name: p for p in schema.parameters}

    assert param_dict["path"].description == "Parameter path"
    assert param_dict["name"].description == "Parameter name"

    await tm.cleanup()
