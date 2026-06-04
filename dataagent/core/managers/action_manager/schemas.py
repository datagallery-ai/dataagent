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
import inspect
import types
from dataclasses import dataclass
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, create_model


class ParameterType(Enum):
    """参数类型"""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    LIST = "list"
    DICT = "dict"


@dataclass
class ParameterSchema:
    """参数Schema"""

    name: str
    type: type
    required: bool = True
    default: Any = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "name": self.name,
            "type": self._type_to_string(),
            "required": self.required,
            "default": self.default,
            "description": self.description,
        }

    def type_to_string(self) -> str:
        """公开方法：将Python类型转换为字符串"""
        return self._type_to_string()

    def _type_to_string(self) -> str:
        """将Python类型转换为JSON Schema类型字符串"""
        tp = self.type
        if tp is None:
            return "string"

        origin = get_origin(tp)

        # 非泛型类型（普通类型）
        if origin is None:
            if tp is int:
                return "integer"
            if tp is float:
                return "float"
            if tp is bool:
                return "boolean"
            if tp is list:
                return "array"
            if tp is dict:
                return "object"
            return "string"

        # 处理泛型类型
        # list[str], dict[str, int] 等 PEP 585 泛型
        if isinstance(origin, type):
            if origin is list:
                return "array"
            if origin is dict:
                return "object"

        # typing.List[str], typing.Dict[str, int] 等
        if origin is list:
            return "array"
        if origin is dict:
            return "object"

        # Union 类型 (typing.Union | str | int) 或 (str | int)
        if origin is Union or origin is types.UnionType:
            args = get_args(tp)
            if args:
                # 取第一个非 None 的参数作为主类型
                for arg in args:
                    if arg is type(None):
                        continue
                    result = ParameterSchema(name="", type=arg).type_to_string()
                    if result != "string":
                        return result
            return "string"

        return "string"


class ToolSchema:
    """工具Schema"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: list[ParameterSchema],
        tool_type: str = "custom",
        metadata: dict[str, Any] = None,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.tool_type = tool_type
        self.metadata = metadata or {}

    @staticmethod
    def _json_type_to_python_type(json_type: str) -> type:
        """将JSON Schema类型转换为Python类型"""
        type_mapping = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
        return type_mapping.get(json_type, str)

    @classmethod
    def from_function(cls, func: callable, name: str | None = None, description: str | None = None) -> "ToolSchema":
        """从函数生成Schema"""
        if name is None:
            name = func.__name__
        if description is None:
            description = func.__doc__ or f"Function {name}"

        signature = inspect.signature(func)
        type_hints = get_type_hints(func)
        parameters = []

        for param_name, param in signature.parameters.items():
            # Internal parameters (e.g. _tool_context) must not appear in LLM tool schema.
            if param_name.startswith("_"):
                continue
            param_type = type_hints.get(param_name, str)
            required = param.default == inspect.Parameter.empty
            default = param.default if param.default != inspect.Parameter.empty else None

            parameters.append(
                ParameterSchema(
                    name=param_name,
                    type=param_type,
                    required=required,
                    default=default,
                    description=f"Parameter {param_name}",
                )
            )

        return cls(name, description, parameters, "local_function")

    @classmethod
    def from_mcp_tool(cls, tool_definition: dict[str, Any], server_id: str) -> "ToolSchema":
        """从MCP工具定义生成Schema"""
        name = tool_definition.get("name", "unknown_mcp_tool")
        description = tool_definition.get("description", f"MCP tool: {name}")
        input_schema = tool_definition.get("inputSchema", {})

        parameters = []
        if "properties" in input_schema:
            required_fields = input_schema.get("required", [])

            for prop_name, prop_def in input_schema["properties"].items():
                param_type = cls._json_type_to_python_type(prop_def.get("type", "string"))
                is_required = prop_name in required_fields
                default_value = prop_def.get("default")
                param_description = prop_def.get("description", f"Parameter {prop_name}")

                parameters.append(
                    ParameterSchema(
                        name=prop_name,
                        type=param_type,
                        required=is_required,
                        default=default_value,
                        description=param_description,
                    )
                )

        metadata = {"server_id": server_id, "original_definition": tool_definition}

        return cls(name, description, parameters, "mcp_tool", metadata)

    @classmethod
    def from_a2a_tool(cls, tool_definition: dict[str, Any], agent_id: str) -> "ToolSchema":
        """从A2A工具定义生成Schema"""
        name = tool_definition.get("name", "unknown_a2a_tool")
        description = tool_definition.get("description", f"A2A tool: {name}")
        parameters_schema = tool_definition.get("parameters", {})

        parameters = []
        if "properties" in parameters_schema:
            required_fields = parameters_schema.get("required", [])

            for prop_name, prop_def in parameters_schema["properties"].items():
                param_type = cls._json_type_to_python_type(prop_def.get("type", "string"))
                is_required = prop_name in required_fields
                default_value = prop_def.get("default")
                param_description = prop_def.get("description", f"Parameter {prop_name}")

                parameters.append(
                    ParameterSchema(
                        name=prop_name,
                        type=param_type,
                        required=is_required,
                        default=default_value,
                        description=param_description,
                    )
                )

        metadata = {"agent_id": agent_id, "original_definition": tool_definition}

        return cls(name, description, parameters, "a2a_tool", metadata)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        result = {
            "name": self.name,
            "description": self.description,
            "parameters": [p.to_dict() for p in self.parameters],
            "tool_type": self.tool_type,
        }

        # 添加元数据
        if self.metadata:
            result["metadata"] = self.metadata

        return result

    def to_openai_function(self) -> dict[str, Any]:
        """转换为OpenAI Function格式"""
        properties = {}
        required = []

        for param in self.parameters:
            properties[param.name] = {"type": param.type_to_string(), "description": param.description}
            if param.required:
                required.append(param.name)

        return {
            "name": self.name,
            "description": self.description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        }

    def to_pydantic_model(self) -> type[BaseModel]:
        """转换为Pydantic模型"""
        fields = {}
        for param in self.parameters:
            if param.required:
                fields[param.name] = (param.type, ...)
            else:
                fields[param.name] = (param.type, param.default)

        return create_model(f"{self.name}Schema", **fields)

    def to_metadata(self) -> dict[str, str]:
        """转换为元数据"""
        return docstring_to_metadata(self.description, self.tool_type)

    def validate_input(self, input_data: dict[str, Any]) -> tuple[bool, str]:
        """验证输入数据"""
        try:
            model = self.to_pydantic_model()
            model(**input_data)
            return (True, None)
        except Exception as e:
            return (False, str(e))


def docstring_to_metadata(docstring: str, tool_type: str = "tool") -> dict[str, str]:
    """Extract function docstring and convert it to a dictionary.

    Args:
        docstring (str): The docstring to be parsed.

    Returns:
        dict[str, str]: Parsed tool metadata with type, description, parameters, output.
    """
    doc = inspect.cleandoc(docstring) or ""
    lines = doc.splitlines()
    description_lines, args_lines, returns_lines = [], [], []
    mode = "description"
    for line in lines:
        s = line.rstrip()
        if s.strip() in {"Args:", "Parameters:"}:
            mode = "args"
            continue
        if s.strip() == "Returns:":
            mode = "returns"
            continue
        if s.strip() in {"Raises:", "Examples:"}:
            break
        if mode == "description":
            if s.strip() == "":
                continue
            description_lines.append(s.strip())
        elif mode == "args":
            if s.strip() == "":
                continue
            args_lines.append(s.strip())
        elif mode == "returns":
            if s.strip() == "":
                continue
            returns_lines.append(s.strip())
    return {
        "type": tool_type,
        "description": "\n".join(description_lines),
        "parameters": "\n".join(args_lines),
        "output": "\n".join(returns_lines),
    }
