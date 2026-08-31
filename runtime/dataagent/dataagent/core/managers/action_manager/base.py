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
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

import httpx

from dataagent.core.errors import DataAgentError

if TYPE_CHECKING:
    from dataagent.core.managers.action_manager.schemas import ToolSchema


class ToolType(Enum):
    """工具类型"""

    LOCAL_FUNCTION = "local_function"
    MCP_TOOL = "mcp_tool"
    A2A_TOOL = "a2a_tool"  # Agent-to-Agent tool
    CUSTOM = "custom"


class ErrorType(StrEnum):
    """错误类型枚举，用于分类错误并决定重试策略"""

    VALIDATION_ERROR = "validation_error"
    AUTHENTICATION_ERROR = "authentication_error"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    INTERNAL_ERROR = "internal_error"
    FILE_NOT_FOUND = "file_not_found"
    UNKNOWN = "unknown"


@dataclass
class ErrorPolicy:
    """错误重试策略"""

    error_type: ErrorType
    retriable: bool
    max_retries: int
    backoff_base: float = 1.0
    backoff_type: str = "exponential"


ERROR_POLICIES: dict[ErrorType, ErrorPolicy] = {
    ErrorType.VALIDATION_ERROR: ErrorPolicy(ErrorType.VALIDATION_ERROR, retriable=False, max_retries=0),
    ErrorType.AUTHENTICATION_ERROR: ErrorPolicy(ErrorType.AUTHENTICATION_ERROR, retriable=False, max_retries=0),
    ErrorType.RATE_LIMIT: ErrorPolicy(
        ErrorType.RATE_LIMIT, retriable=True, max_retries=3, backoff_base=1.0, backoff_type="exponential"
    ),
    ErrorType.TIMEOUT: ErrorPolicy(
        ErrorType.TIMEOUT, retriable=True, max_retries=1, backoff_base=2.0, backoff_type="fixed"
    ),
    ErrorType.NETWORK_ERROR: ErrorPolicy(
        ErrorType.NETWORK_ERROR, retriable=True, max_retries=3, backoff_base=1.0, backoff_type="exponential"
    ),
    ErrorType.INTERNAL_ERROR: ErrorPolicy(
        ErrorType.INTERNAL_ERROR, retriable=True, max_retries=1, backoff_base=1.0, backoff_type="fixed"
    ),
    ErrorType.FILE_NOT_FOUND: ErrorPolicy(ErrorType.FILE_NOT_FOUND, retriable=False, max_retries=0),
    ErrorType.UNKNOWN: ErrorPolicy(
        ErrorType.UNKNOWN, retriable=True, max_retries=1, backoff_base=1.0, backoff_type="fixed"
    ),
}

DEFAULT_RETRY_POLICY = ERROR_POLICIES[ErrorType.UNKNOWN]


def classify_exception(exc: Exception) -> tuple[ErrorType, ErrorPolicy]:
    """Classify an exception by type and HTTP status. Do not scan fact or messages."""
    if exc is None:
        raise TypeError("classify_exception received None, expected an Exception")

    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return (ErrorType.TIMEOUT, ERROR_POLICIES[ErrorType.TIMEOUT])
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 401:
            return (ErrorType.AUTHENTICATION_ERROR, ERROR_POLICIES[ErrorType.AUTHENTICATION_ERROR])
        if status_code == 429:
            return (ErrorType.RATE_LIMIT, ERROR_POLICIES[ErrorType.RATE_LIMIT])
        return (ErrorType.UNKNOWN, DEFAULT_RETRY_POLICY)
    if isinstance(exc, (ConnectionError, httpx.RequestError)):
        return (ErrorType.NETWORK_ERROR, ERROR_POLICIES[ErrorType.NETWORK_ERROR])
    if type(exc).__name__ == "ParamsValueError":
        return (ErrorType.VALIDATION_ERROR, ERROR_POLICIES[ErrorType.VALIDATION_ERROR])

    return (ErrorType.UNKNOWN, DEFAULT_RETRY_POLICY)


@dataclass
class ToolResult:
    """工具执行结果；``error is None`` 表示成功。"""

    data: Any = None
    error: DataAgentError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.error is None


def tool_failure(
    *,
    fact: str | None = None,
    source: str = "tool",
    component: str = "tool",
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    """Build a failed ToolResult with a structured DataAgentError."""
    return ToolResult(
        error=DataAgentError(
            source=source,
            fact=fact,
            component=component,
        ),
        metadata=metadata or {},
    )


class BaseTool(ABC):
    """工具基类"""

    def __init__(self, name: str, category: str = "general", description: str = "", **kwargs):
        self.name = name
        self.category = category
        self.description = description
        self.config = kwargs
        self.tool_type = ToolType.CUSTOM
        self.pre_hooks: list = []
        self.post_hooks: list = []

    @abstractmethod
    def call(self, **kwargs) -> ToolResult:
        """执行工具"""
        pass

    @abstractmethod
    def get_schema(self) -> "ToolSchema":
        """获取工具Schema"""
        pass

    def to_langchain_tool(self):
        """转换为LangChain工具"""
        from langchain_core.tools import StructuredTool

        def tool_func(**kwargs):
            result = self.call(**kwargs)
            if result.success:
                return result.data
            raise result.error or DataAgentError(source="tool", component="tool")

        return StructuredTool.from_function(
            func=tool_func,
            name=self.name,
            description=self.description,
            args_schema=self.get_schema().to_pydantic_model(),
        )

    def validate_input(self, **kwargs) -> tuple[bool, str | None]:
        """验证输入参数"""
        schema = self.get_schema()
        return schema.validate_input(kwargs)
