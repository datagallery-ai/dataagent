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
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

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

    VALIDATION_ERROR = "validation_error"  # 参数 schema 校验失败
    RATE_LIMIT = "rate_limit"  # 限流/配额耗尽
    TIMEOUT = "timeout"  # 工具执行超时
    NETWORK_ERROR = "network_error"  # 网络问题（MCP/A2A）
    INTERNAL_ERROR = "internal_error"  # 工具内部异常
    FILE_NOT_FOUND = "file_not_found"  # 文件/路径不存在
    UNKNOWN = "unknown"  # 未知错误


@dataclass
class ErrorPolicy:
    """错误重试策略"""

    error_type: ErrorType
    retriable: bool
    max_retries: int
    backoff_base: float = 1.0  # 退避基数（秒）
    backoff_type: str = "exponential"  # exponential 或 fixed


# 默认重试策略表
ERROR_POLICIES: dict[ErrorType, ErrorPolicy] = {
    ErrorType.VALIDATION_ERROR: ErrorPolicy(ErrorType.VALIDATION_ERROR, retriable=False, max_retries=0),
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
    """统一错误分类函数，根据异常类型和消息内容分类错误。

    Args:
        exc: 待分类的异常对象

    Returns:
        (ErrorType, ErrorPolicy) 元组，包含错误类型和对应的重试策略

    Raises:
        TypeError: 当 exc 为 None 时抛出

    Examples:
        >>> exc = TimeoutError("Request timed out")
        >>> err_type, policy = classify_exception(exc)
        >>> print(err_type)
        ErrorType.TIMEOUT
    """
    if exc is None:
        raise TypeError("classify_exception received None, expected an Exception")
    exc_type = type(exc).__name__.lower()
    exc_msg = str(exc).lower()  # 大小写不敏感匹配

    # 错误分类规则（按优先级顺序匹配）
    if "timeout" in exc_type or "timeout" in exc_msg or "timed out" in exc_msg or "deadline" in exc_msg:
        return (ErrorType.TIMEOUT, ERROR_POLICIES[ErrorType.TIMEOUT])
    if "rate limit" in exc_msg or "quota" in exc_msg or "too many" in exc_msg or "429" in exc_msg:
        return (ErrorType.RATE_LIMIT, ERROR_POLICIES[ErrorType.RATE_LIMIT])
    if (
        "file not found" in exc_msg
        or "no such file" in exc_msg
        or "does not exist" in exc_msg
        or "command not found" in exc_msg
    ):
        return (ErrorType.FILE_NOT_FOUND, ERROR_POLICIES[ErrorType.FILE_NOT_FOUND])
    if (
        "network" in exc_type
        or "network" in exc_msg
        or "connection" in exc_msg
        or "dns" in exc_msg
        or "refused" in exc_msg
        or "unreachable" in exc_msg
    ):
        return (ErrorType.NETWORK_ERROR, ERROR_POLICIES[ErrorType.NETWORK_ERROR])
    if "validation" in exc_msg or "invalid" in exc_msg or "schema" in exc_msg or "param" in exc_msg:
        return (ErrorType.VALIDATION_ERROR, ERROR_POLICIES[ErrorType.VALIDATION_ERROR])
    if "internal" in exc_msg or "unexpected" in exc_msg or "assertion" in exc_msg or "panic" in exc_msg:
        return (ErrorType.INTERNAL_ERROR, ERROR_POLICIES[ErrorType.INTERNAL_ERROR])

    return (ErrorType.UNKNOWN, DEFAULT_RETRY_POLICY)


@dataclass
class ToolResult:
    """工具执行结果"""

    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = None
    error_type: ErrorType | None = None  # 错误类型
    retriable: bool | None = None  # 是否可重试
    max_retries: int = 0  # 最大重试次数

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class ToolError(Exception):
    """工具错误，支持错误分类和重试信息"""

    def __init__(
        self,
        message: str,
        error_type: ErrorType = ErrorType.UNKNOWN,
        retriable: bool | None = None,
        max_retries: int | None = None,
        **kwargs,
    ):
        self.message = message
        self.error_type = error_type
        # 如果没有显式指定 retriable，从策略推断
        if retriable is None:
            self.retriable = ERROR_POLICIES.get(error_type, DEFAULT_RETRY_POLICY).retriable
        else:
            self.retriable = retriable
        # 如果没有显式指定 max_retries，从策略推断
        if max_retries is None:
            self.max_retries = ERROR_POLICIES.get(error_type, DEFAULT_RETRY_POLICY).max_retries
        else:
            self.max_retries = max_retries
        self.kwargs = kwargs
        super().__init__(message)

    def __str__(self):
        return self.message


class BaseTool(ABC):
    """工具基类"""

    def __init__(self, name: str, category: str = "general", description: str = "", **kwargs):
        self.name = name
        self.category = category
        self.description = description
        self.config = kwargs
        self.tool_type = ToolType.CUSTOM

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

            raise ToolError(result.error)

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
