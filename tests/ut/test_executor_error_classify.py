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
"""Executor 错误分类单元测试"""

import pytest

from dataagent.actions.environment import Env
from dataagent.core.flex.nodes.executor import Executor
from dataagent.core.managers.action_manager.base import (
    DEFAULT_RETRY_POLICY,
    ERROR_POLICIES,
    ErrorType,
    ToolError,
)


class TestExecutorClassifyError:
    """Executor._classify_error 方法测试"""

    @pytest.fixture
    def executor(self):
        """创建 Executor 实例"""
        env = Env()
        return Executor(name="test_executor", env=env)

    # ===== ToolError 异常测试 =====

    def test_tool_error_validation_error(self, executor):
        """测试 ToolError 的 VALIDATION_ERROR"""
        error = ToolError(message="Validation failed", error_type=ErrorType.VALIDATION_ERROR)
        policy = executor._classify_error(error)
        assert policy == ERROR_POLICIES[ErrorType.VALIDATION_ERROR]
        assert policy.retriable is False

    def test_tool_error_rate_limit(self, executor):
        """测试 ToolError 的 RATE_LIMIT"""
        error = ToolError(message="Rate limit exceeded", error_type=ErrorType.RATE_LIMIT)
        policy = executor._classify_error(error)
        assert policy == ERROR_POLICIES[ErrorType.RATE_LIMIT]
        assert policy.retriable is True

    def test_tool_error_timeout(self, executor):
        """测试 ToolError 的 TIMEOUT"""
        error = ToolError(message="Operation timed out", error_type=ErrorType.TIMEOUT)
        policy = executor._classify_error(error)
        assert policy == ERROR_POLICIES[ErrorType.TIMEOUT]
        assert policy.retriable is True

    def test_tool_error_network_error(self, executor):
        """测试 ToolError 的 NETWORK_ERROR"""
        error = ToolError(message="Network connection failed", error_type=ErrorType.NETWORK_ERROR)
        policy = executor._classify_error(error)
        assert policy == ERROR_POLICIES[ErrorType.NETWORK_ERROR]
        assert policy.retriable is True

    def test_tool_error_internal_error(self, executor):
        """测试 ToolError 的 INTERNAL_ERROR"""
        error = ToolError(message="Internal server error", error_type=ErrorType.INTERNAL_ERROR)
        policy = executor._classify_error(error)
        assert policy == ERROR_POLICIES[ErrorType.INTERNAL_ERROR]
        assert policy.retriable is True

    def test_tool_error_file_not_found(self, executor):
        """测试 ToolError 的 FILE_NOT_FOUND"""
        error = ToolError(message="File not found", error_type=ErrorType.FILE_NOT_FOUND)
        policy = executor._classify_error(error)
        assert policy == ERROR_POLICIES[ErrorType.FILE_NOT_FOUND]
        assert policy.retriable is False

    def test_tool_error_unknown(self, executor):
        """测试 ToolError 的 UNKNOWN 类型"""
        error = ToolError(message="Something went wrong", error_type=ErrorType.UNKNOWN)
        policy = executor._classify_error(error)
        assert policy == ERROR_POLICIES[ErrorType.UNKNOWN]
        assert policy.retriable is True

    # ===== 关键词匹配测试 =====

    def test_keyword_timeout(self, executor):
        """测试 timeout 关键词"""
        keywords = ["timeout", "timed out", "deadline"]
        for keyword in keywords:
            error = Exception(f"Operation {keyword}")
            policy = executor._classify_error(error)
            assert policy.error_type == ErrorType.TIMEOUT, f"Failed for keyword: {keyword}"

    def test_keyword_timeout_case_insensitive(self, executor):
        """测试 timeout 关键词大小写不敏感"""
        error = Exception("Request TIMEOUT error")
        policy = executor._classify_error(error)
        assert policy.error_type == ErrorType.TIMEOUT

    def test_keyword_rate_limit(self, executor):
        """测试 rate limit 关键词"""
        keywords = ["rate limit", "quota exceeded", "too many requests", "429"]
        for keyword in keywords:
            error = Exception(f"Error: {keyword}")
            policy = executor._classify_error(error)
            assert policy.error_type == ErrorType.RATE_LIMIT, f"Failed for keyword: {keyword}"

    def test_keyword_rate_limit_case_insensitive(self, executor):
        """测试 rate limit 关键词大小写不敏感"""
        error = Exception("RATE LIMIT exceeded")
        policy = executor._classify_error(error)
        assert policy.error_type == ErrorType.RATE_LIMIT

    def test_keyword_file_not_found(self, executor):
        """测试 file not found 关键词"""
        keywords = ["not found", "no such file", "does not exist"]
        for keyword in keywords:
            error = Exception(f"Path {keyword}")
            policy = executor._classify_error(error)
            assert policy.error_type == ErrorType.FILE_NOT_FOUND, f"Failed for keyword: {keyword}"

    def test_keyword_file_not_found_case_insensitive(self, executor):
        """测试 file not found 关键词大小写不敏感"""
        error = Exception("File NOT FOUND at path")
        policy = executor._classify_error(error)
        assert policy.error_type == ErrorType.FILE_NOT_FOUND

    def test_keyword_network_error(self, executor):
        """测试 network error 关键词"""
        # connection 在 exc_msg 中需要与 connection 相关异常类型配合，或使用其他已知关键词
        # 改用 dns/refused/unreachable 等可直接在 exc_msg 中识别的关键词
        keywords = ["dns resolution failed", "connection refused", "host unreachable"]
        for keyword in keywords:
            error = Exception(f"Failed to connect: {keyword}")
            policy = executor._classify_error(error)
            assert policy.error_type == ErrorType.NETWORK_ERROR, f"Failed for keyword: {keyword}"

    def test_keyword_network_error_case_insensitive(self, executor):
        """测试 network error 关键词大小写不敏感"""
        # 使用在 exc_msg 中可识别的 connection 相关错误
        error = Exception("Connection refused - host unreachable")
        policy = executor._classify_error(error)
        assert policy.error_type == ErrorType.NETWORK_ERROR

    def test_keyword_validation_error(self, executor):
        """测试 validation error 关键词"""
        keywords = ["validation", "invalid", "schema"]
        for keyword in keywords:
            error = Exception(f"Parameter {keyword} failed")
            policy = executor._classify_error(error)
            assert policy.error_type == ErrorType.VALIDATION_ERROR, f"Failed for keyword: {keyword}"

    def test_keyword_validation_error_case_insensitive(self, executor):
        """测试 validation error 关键词大小写不敏感"""
        error = Exception("VALIDATION of parameters failed")
        policy = executor._classify_error(error)
        assert policy.error_type == ErrorType.VALIDATION_ERROR

    def test_keyword_internal_error(self, executor):
        """测试 internal error 关键词"""
        keywords = ["internal", "unexpected", "assertion", "panic"]
        for keyword in keywords:
            error = Exception(f"Server {keyword} error")
            policy = executor._classify_error(error)
            assert policy.error_type == ErrorType.INTERNAL_ERROR, f"Failed for keyword: {keyword}"

    def test_keyword_internal_error_case_insensitive(self, executor):
        """测试 internal error 关键词大小写不敏感"""
        error = Exception("INTERNAL server error")
        policy = executor._classify_error(error)
        assert policy.error_type == ErrorType.INTERNAL_ERROR

    # ===== 优先级测试 =====

    def test_tool_error_takes_precedence(self, executor):
        """测试 ToolError 优先于关键词匹配"""
        # 虽然消息包含 "timeout"，但 error_type 是 NETWORK_ERROR
        error = ToolError(
            message="Connection timeout occurred",
            error_type=ErrorType.NETWORK_ERROR,
        )
        policy = executor._classify_error(error)
        assert policy.error_type == ErrorType.NETWORK_ERROR

    def test_first_match_priority(self, executor):
        """测试匹配顺序：按 if 顺序优先匹配"""
        # 消息同时包含多种关键词，但按顺序匹配
        error = Exception("timeout rate limit quota exceeded")
        policy = executor._classify_error(error)
        # timeout 在前面，会优先匹配
        assert policy.error_type == ErrorType.TIMEOUT

    # ===== 默认值测试 =====

    def test_unknown_error_returns_default_policy(self, executor):
        """测试未知错误返回默认策略"""
        error = Exception("Some random error message")
        policy = executor._classify_error(error)
        assert policy == DEFAULT_RETRY_POLICY
        assert policy == ERROR_POLICIES[ErrorType.UNKNOWN]

    def test_empty_message(self, executor):
        """测试空消息"""
        error = Exception("")
        policy = executor._classify_error(error)
        assert policy == DEFAULT_RETRY_POLICY

    def test_non_string_in_message(self, executor):
        """测试消息中包含非字符串内容"""
        error = Exception(12345)
        policy = executor._classify_error(error)
        # str(12345) = "12345"，不包含任何关键词
        assert policy == DEFAULT_RETRY_POLICY
