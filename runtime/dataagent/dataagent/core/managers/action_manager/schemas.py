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
import re
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


_JSON_TYPE_TO_PYTHON: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def json_type_to_python_type(json_type: str) -> type:
    """将单个JSON Schema类型名转换为Python类型（未知类型退化为str）"""
    return _JSON_TYPE_TO_PYTHON.get(json_type, str)


def _resolve_json_schema_type(prop_def: Any) -> tuple[type | None, bool]:
    """Read one property as ``(concrete type or None, accepts null)``.

    ``None`` for the type means the property constrains nothing, which is not
    the same as ``"null"`` — that constrains it to null alone.
    """
    if not isinstance(prop_def, dict):
        return (None, False)

    declared = prop_def.get("type")
    if isinstance(declared, str):
        if declared == "null":
            return (None, True)
        return (json_type_to_python_type(declared), False)
    if isinstance(declared, list):
        nullable = "null" in declared
        for entry in declared:
            if entry != "null":
                return (json_type_to_python_type(entry), nullable)
        return (None, nullable)

    for keyword in ("anyOf", "oneOf"):
        branches = prop_def.get(keyword)
        if not isinstance(branches, list):
            continue
        resolved: type | None = None
        nullable = False
        for branch in branches:
            branch_type, branch_nullable = _resolve_json_schema_type(branch)
            nullable = nullable or branch_nullable
            if resolved is None:
                resolved = branch_type
        return (resolved, nullable)

    return (None, False)


def python_type_from_json_schema(prop_def: Any) -> Any:
    """Resolve the Python type a JSON Schema property accepts.

    Covers the shapes a tool's ``inputSchema`` actually arrives in:

    - ``{"type": "integer"}`` -> ``int``
    - ``{"type": ["integer", "null"]}`` -> ``int | None``
    - ``{"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None}``
      -> ``int | None``, which is what FastMCP emits for an ``Optional[int]``
      parameter; ``oneOf`` is read the same way
    - a property that declares no type at all -> ``Any``

    Reading only the top-level ``type`` key is what made all of these degrade to
    ``str``: none of them has that key, so every one of them came back a string
    and then rejected the integer the model was supposed to send. An untyped
    property becomes ``Any`` for the same reason — with no declared type there
    is nothing to narrow it to, and ``str`` is a narrowing.

    The first non-null branch wins. A union of several concrete types has no
    single ``ParameterSchema.type`` to be, and taking the first keeps the type
    the model is shown in the order the server declared it.
    """
    resolved, nullable = _resolve_json_schema_type(prop_def)
    if resolved is None:
        # Unconstrained already admits null; there is nothing to widen.
        return Any
    if nullable or _declares_null_default(prop_def):
        return resolved | None
    return resolved


def _declares_null_default(prop_def: Any) -> bool:
    """Whether a property defaults to null, which is a null it has to accept."""
    return isinstance(prop_def, dict) and "default" in prop_def and prop_def["default"] is None


def parameter_from_json_schema_property(prop_name: str, prop_def: Any, *, required: bool) -> ParameterSchema:
    """Build one :class:`ParameterSchema` from a JSON Schema property definition."""
    definition = prop_def if isinstance(prop_def, dict) else {}
    return ParameterSchema(
        name=prop_name,
        type=python_type_from_json_schema(definition),
        required=required,
        default=definition.get("default"),
        description=definition.get("description", f"Parameter {prop_name}"),
    )


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
        return json_type_to_python_type(json_type)

    @classmethod
    def from_function(cls, func: callable, name: str | None = None) -> "ToolSchema":
        """从函数生成Schema"""
        if name is None:
            name = func.__name__

        docstring = func.__doc__ or f"Function {name}"
        metadata = docstring_to_metadata(docstring)
        description = metadata.get("description") or docstring

        signature = inspect.signature(func)
        type_hints = get_type_hints(func)
        param_descriptions = cls._parse_docstring_param_descriptions(metadata.get("parameters", ""))
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
                    description=param_descriptions.get(param_name, f"Parameter {param_name}"),
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
                parameters.append(
                    parameter_from_json_schema_property(prop_name, prop_def, required=prop_name in required_fields)
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
                parameters.append(
                    parameter_from_json_schema_property(prop_name, prop_def, required=prop_name in required_fields)
                )

        metadata = {"agent_id": agent_id, "original_definition": tool_definition}

        return cls(name, description, parameters, "a2a_tool", metadata)

    @classmethod
    def _parse_docstring_param_descriptions(cls, args_section: str) -> dict[str, str]:
        """Parse parameter descriptions from Args section string.

        Handles formats like:
            a (str): The first parameter.
            b (int): The second parameter.
        """
        result: dict[str, str] = {}
        if not args_section:
            return result
        lines = args_section.splitlines()
        current_param = None
        current_desc = []

        for line in lines:
            s = line.rstrip()
            if not s.strip():
                continue
            param_match = re.match(r"^\s*(\w+)\s*[:(]", s)
            if param_match:
                if current_param:
                    result[current_param] = " ".join(current_desc).strip()
                current_param = param_match.group(1)
                desc_part = s[s.index(":") + 1 :].strip() if ":" in s else s[s.index("(") + 1 : s.index(")")].strip()
                current_desc = [desc_part] if desc_part else []
            elif current_param and (s.startswith(("    ", "\t", " " * 8)) or not s[0].isalnum()):
                current_desc.append(s.strip())
            elif current_param:
                if s.startswith("-"):
                    continue
                current_desc.append(s.strip())

        if current_param:
            result[current_param] = " ".join(current_desc).strip()
        return result

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
