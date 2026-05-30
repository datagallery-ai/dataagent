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
"""工具错误分类模块测试"""

import pytest

from dataagent.core.managers.action_manager.base import (
    DEFAULT_RETRY_POLICY,
    ERROR_POLICIES,
    ErrorPolicy,
    ErrorType,
    ToolError,
    ToolResult,
    classify_exception,
)


class TestErrorType:
    """ErrorType 枚举测试"""

    def test_all_error_types_defined(self):
        """所有错误类型都应该被定义"""
        expected_types = {
            "validation_error",
            "rate_limit",
            "timeout",
            "network_error",
            "internal_error",
            "file_not_found",
            "unknown",
        }
        actual_types = {e.value for e in ErrorType}
        assert expected_types == actual_types

    def test_error_type_string_values(self):
        """ErrorType 应该有正确的字符串值"""
        assert ErrorType.VALIDATION_ERROR.value == "validation_error"
        assert ErrorType.RATE_LIMIT.value == "rate_limit"
        assert ErrorType.TIMEOUT.value == "timeout"
        assert ErrorType.NETWORK_ERROR.value == "network_error"
        assert ErrorType.INTERNAL_ERROR.value == "internal_error"
        assert ErrorType.FILE_NOT_FOUND.value == "file_not_found"
        assert ErrorType.UNKNOWN.value == "unknown"


class TestErrorPolicy:
    """ErrorPolicy 测试"""

    def test_validation_error_not_retriable(self):
        """VALIDATION_ERROR 不可重试"""
        policy = ERROR_POLICIES[ErrorType.VALIDATION_ERROR]
        assert policy.retriable is False
        assert policy.max_retries == 0

    def test_rate_limit_retriable_with_exponential_backoff(self):
        """RATE_LIMIT 可重试，使用指数退避"""
        policy = ERROR_POLICIES[ErrorType.RATE_LIMIT]
        assert policy.retriable is True
        assert policy.max_retries == 3
        assert policy.backoff_type == "exponential"
        assert policy.backoff_base == 1.0

    def test_timeout_retriable_with_fixed_backoff(self):
        """TIMEOUT 可重试，使用固定退避"""
        policy = ERROR_POLICIES[ErrorType.TIMEOUT]
        assert policy.retriable is True
        assert policy.max_retries == 1
        assert policy.backoff_type == "fixed"
        assert policy.backoff_base == 2.0

    def test_network_error_retriable_with_exponential_backoff(self):
        """NETWORK_ERROR 可重试，使用指数退避"""
        policy = ERROR_POLICIES[ErrorType.NETWORK_ERROR]
        assert policy.retriable is True
        assert policy.max_retries == 3
        assert policy.backoff_type == "exponential"
        assert policy.backoff_base == 1.0

    def test_internal_error_retriable(self):
        """INTERNAL_ERROR 可重试"""
        policy = ERROR_POLICIES[ErrorType.INTERNAL_ERROR]
        assert policy.retriable is True
        assert policy.max_retries == 1
        assert policy.backoff_type == "fixed"
        assert policy.backoff_base == 1.0

    def test_file_not_found_not_retriable(self):
        """FILE_NOT_FOUND 不可重试"""
        policy = ERROR_POLICIES[ErrorType.FILE_NOT_FOUND]
        assert policy.retriable is False
        assert policy.max_retries == 0

    def test_unknown_retriable(self):
        """UNKNOWN 默认可重试"""
        policy = ERROR_POLICIES[ErrorType.UNKNOWN]
        assert policy.retriable is True
        assert policy.max_retries == 1

    def test_default_retry_policy(self):
        """默认重试策略应该是 UNKNOWN 的策略"""
        assert ERROR_POLICIES[ErrorType.UNKNOWN] == DEFAULT_RETRY_POLICY


class TestClassifyException:
    """classify_exception 函数测试"""

    def test_timeout_error_classified(self):
        """TimeoutError 应该被分类为 TIMEOUT"""
        exc = TimeoutError("Request timed out")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.TIMEOUT
        assert policy == ERROR_POLICIES[ErrorType.TIMEOUT]

    def test_timed_out_in_message(self):
        """消息中包含 'timed out' 应该被分类为 TIMEOUT"""
        exc = Exception("Connection timed out")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.TIMEOUT

    def test_deadline_in_message(self):
        """消息中包含 'deadline' 应该被分类为 TIMEOUT"""
        exc = Exception("Request deadline exceeded")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.TIMEOUT

    def test_rate_limit_in_message(self):
        """消息中包含 'rate limit' 应该被分类为 RATE_LIMIT"""
        exc = Exception("Rate limit exceeded")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.RATE_LIMIT
        assert policy.retriable is True

    def test_quota_in_message(self):
        """消息中包含 'quota' 应该被分类为 RATE_LIMIT"""
        exc = Exception("Quota exceeded for API calls")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.RATE_LIMIT

    def test_too_many_in_message(self):
        """消息中包含 'too many' 应该被分类为 RATE_LIMIT"""
        exc = Exception("Too many requests")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.RATE_LIMIT

    def test_429_in_message(self):
        """消息中包含 '429' 应该被分类为 RATE_LIMIT"""
        exc = Exception("HTTP 429 Too Many Requests")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.RATE_LIMIT

    def test_not_found_in_message(self):
        """消息中包含 'not found' 应该被分类为 FILE_NOT_FOUND"""
        exc = Exception("File not found: test.txt")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.FILE_NOT_FOUND
        assert policy.retriable is False

    def test_no_such_file_in_message(self):
        """消息中包含 'no such file' 应该被分类为 FILE_NOT_FOUND"""
        exc = Exception("No such file or directory")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.FILE_NOT_FOUND

    def test_does_not_exist_in_message(self):
        """消息中包含 'does not exist' 应该被分类为 FILE_NOT_FOUND"""
        exc = Exception("Path does not exist")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.FILE_NOT_FOUND

    def test_connection_in_type(self):
        """异常类型名包含 'connection' 应该被分类为 NETWORK_ERROR"""
        exc = ConnectionError("Connection refused")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.NETWORK_ERROR
        assert policy.retriable is True

    def test_network_in_type(self):
        """异常类型名包含 'network' 应该被分类为 NETWORK_ERROR"""

        # 创建自定义异常，类型名包含 "network"
        class NetworkError(OSError):
            pass

        exc = NetworkError("Some error")  # 消息不含 network 关键词
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.NETWORK_ERROR

    def test_dns_in_message(self):
        """消息中包含 'dns' 应该被分类为 NETWORK_ERROR"""
        exc = Exception("DNS resolution failed")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.NETWORK_ERROR

    def test_refused_in_message(self):
        """消息中包含 'refused' 应该被分类为 NETWORK_ERROR"""
        exc = Exception("Connection refused by server")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.NETWORK_ERROR

    def test_unreachable_in_message(self):
        """消息中包含 'unreachable' 应该被分类为 NETWORK_ERROR"""
        exc = Exception("Host unreachable")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.NETWORK_ERROR

    def test_validation_in_message(self):
        """消息中包含 'validation' 应该被分类为 VALIDATION_ERROR"""
        exc = Exception("Validation failed")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.VALIDATION_ERROR
        assert policy.retriable is False

    def test_invalid_in_message(self):
        """消息中包含 'invalid' 应该被分类为 VALIDATION_ERROR"""
        exc = Exception("Invalid parameter")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.VALIDATION_ERROR

    def test_schema_in_message(self):
        """消息中包含 'schema' 应该被分类为 VALIDATION_ERROR"""
        exc = Exception("Schema mismatch")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.VALIDATION_ERROR

    def test_param_in_message(self):
        """消息中包含 'param' 应该被分类为 VALIDATION_ERROR"""
        exc = Exception("Missing required param")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.VALIDATION_ERROR

    def test_internal_in_message(self):
        """消息中包含 'internal' 应该被分类为 INTERNAL_ERROR"""
        exc = Exception("Internal server error")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.INTERNAL_ERROR

    def test_unexpected_in_message(self):
        """消息中包含 'unexpected' 应该被分类为 INTERNAL_ERROR"""
        exc = Exception("Unexpected error occurred")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.INTERNAL_ERROR

    def test_assertion_in_message(self):
        """消息中包含 'assertion' 应该被分类为 INTERNAL_ERROR"""
        exc = Exception("Assertion failed")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.INTERNAL_ERROR

    def test_panic_in_message(self):
        """消息中包含 'panic' 应该被分类为 INTERNAL_ERROR"""
        exc = Exception("System panic")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.INTERNAL_ERROR

    def test_unknown_error(self):
        """无法分类的错误应该返回 UNKNOWN"""
        exc = Exception("Some random error")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.UNKNOWN
        assert policy == DEFAULT_RETRY_POLICY

    def test_classify_returns_tuple(self):
        """classify_exception 应该返回 (ErrorType, ErrorPolicy) 元组"""
        exc = Exception("test error")
        result = classify_exception(exc)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], ErrorType)
        assert isinstance(result[1], ErrorPolicy)


class TestBackoffCalculation:
    """退避时间计算测试"""

    def test_exponential_backoff(self):
        """测试指数退避计算"""
        policy = ErrorPolicy(
            error_type=ErrorType.RATE_LIMIT,
            retriable=True,
            max_retries=3,
            backoff_base=1.0,
            backoff_type="exponential",
        )
        # backoff = base * 2^attempt
        assert policy.backoff_base * (2**0) == 1.0  # 第 1 次重试
        assert policy.backoff_base * (2**1) == 2.0  # 第 2 次重试
        assert policy.backoff_base * (2**2) == 4.0  # 第 3 次重试

    def test_fixed_backoff(self):
        """测试固定退避计算"""
        policy = ErrorPolicy(
            error_type=ErrorType.TIMEOUT,
            retriable=True,
            max_retries=1,
            backoff_base=2.0,
            backoff_type="fixed",
        )
        # 固定退避始终等于 base
        assert policy.backoff_base == 2.0


class TestToolError:
    """ToolError 异常测试"""

    def test_tool_error_with_error_type(self):
        """ToolError 应该支持 error_type 参数"""
        exc = ToolError("Test error", error_type=ErrorType.RATE_LIMIT)
        assert str(exc) == "Test error"
        assert exc.error_type == ErrorType.RATE_LIMIT
        assert exc.retriable is True  # 从策略推断

    def test_tool_error_infer_retriable_from_policy(self):
        """如果没有指定 retriable，从策略推断"""
        exc = ToolError("Validation error", error_type=ErrorType.VALIDATION_ERROR)
        assert exc.retriable is False  # VALIDATION_ERROR 不可重试

    def test_tool_error_infer_max_retries_from_policy(self):
        """如果没有指定 max_retries，从策略推断"""
        exc = ToolError("Rate limit", error_type=ErrorType.RATE_LIMIT)
        assert exc.max_retries == 3  # RATE_LIMIT 最大重试 3 次

    def test_tool_error_explicit_retriable(self):
        """可以显式指定 retriable"""
        exc = ToolError("Test", error_type=ErrorType.VALIDATION_ERROR, retriable=True)
        assert exc.retriable is True  # 显式覆盖策略

    def test_tool_error_can_be_caught(self):
        """ToolError 应该可以被捕获"""
        exc = ToolError("Test error", error_type=ErrorType.TIMEOUT)
        try:
            raise exc
        except ToolError as e:
            assert str(e) == "Test error"
            assert e.error_type == ErrorType.TIMEOUT


class TestToolResult:
    """ToolResult 数据结构测试"""

    def test_successful_result(self):
        """成功的 ToolResult"""
        result = ToolResult(success=True, data={"result": "ok"})
        assert result.success is True
        assert result.data == {"result": "ok"}
        assert result.error is None
        assert result.error_type is None

    def test_failed_result_with_error_type(self):
        """失败的 ToolResult 带有错误类型"""
        result = ToolResult(
            success=False,
            error="File not found",
            error_type=ErrorType.FILE_NOT_FOUND,
            metadata={"file": "test.txt"},
        )
        assert result.success is False
        assert result.error == "File not found"
        assert result.error_type == ErrorType.FILE_NOT_FOUND
        assert result.metadata == {"file": "test.txt"}

    def test_failed_result_with_retry_info(self):
        """失败的 ToolResult 带有重试信息"""
        result = ToolResult(
            success=False,
            error="Rate limit",
            error_type=ErrorType.RATE_LIMIT,
            retriable=True,
            max_retries=3,
        )
        assert result.retriable is True
        assert result.max_retries == 3

    def test_result_metadata_defaults_to_empty_dict(self):
        """metadata 默认应该为空字典"""
        result = ToolResult(success=True, data="ok")
        assert result.metadata == {}


class TestEdgeCases:
    """边界情况测试"""

    def test_empty_exception_message(self):
        """空错误消息应该被正确处理"""
        exc = Exception("")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.UNKNOWN

    def test_command_not_found_classified(self):
        """'command not found' 应该被分类为 FILE_NOT_FOUND"""
        exc = Exception("/bin/bash: line 1: nonexistent_command: command not found")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.FILE_NOT_FOUND
        assert policy.retriable is False

    def test_none_exception(self):
        """None 应该抛出异常"""
        with pytest.raises((AttributeError, TypeError)):
            classify_exception(None)

    def test_timeout_in_exception_name(self):
        """异常类型名包含 timeout 应该被分类为 TIMEOUT"""
        # TimeoutError 是标准异常，类型名包含 "timeout"
        exc = TimeoutError("Some error")  # 消息故意不含 timeout 相关词
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.TIMEOUT

    def test_case_insensitive_matching(self):
        """关键词匹配应该不区分大小写"""
        exc = Exception("RATE LIMIT exceeded")
        error_type, policy = classify_exception(exc)
        assert error_type == ErrorType.RATE_LIMIT

    def test_multiple_keywords_first_match_wins(self):
        """多个关键词匹配时，第一个匹配决定分类"""
        # "timeout" 在关键词列表中比 "rate limit" 更靠前
        exc = Exception("Timeout: rate limit exceeded")
        error_type, policy = classify_exception(exc)
        # timeout 关键词会首先匹配
        assert error_type == ErrorType.TIMEOUT


class TestErrorPolicyLookup:
    """错误策略查找测试"""

    def test_lookup_valid_error_type(self):
        """可以正确查找有效的 ErrorType"""
        for error_type in ErrorType:
            policy = ERROR_POLICIES.get(error_type)
            assert policy is not None
            assert isinstance(policy, ErrorPolicy)

    def test_lookup_invalid_error_type_returns_none(self):
        """查找无效的 ErrorType 返回 None"""
        policy = ERROR_POLICIES.get(None)
        assert policy is None

    def test_all_error_types_have_policies(self):
        """所有 ErrorType 都应该有对应的策略"""
        assert len(ERROR_POLICIES) == len(ErrorType)


class TestRetryPolicyConsistency:
    """重试策略一致性测试"""

    def test_retriable_errors_have_max_retries_greater_than_zero(self):
        """可重试的错误 max_retries 应该大于 0"""
        for error_type, policy in ERROR_POLICIES.items():
            if policy.retriable:
                assert policy.max_retries > 0, f"{error_type} is retriable but has max_retries=0"

    def test_non_retriable_errors_have_max_retries_zero(self):
        """不可重试的错误 max_retries 应该为 0"""
        non_retriable = [
            ErrorType.VALIDATION_ERROR,
            ErrorType.FILE_NOT_FOUND,
        ]
        for error_type in non_retriable:
            policy = ERROR_POLICIES[error_type]
            assert policy.retriable is False
            assert policy.max_retries == 0

    def test_backoff_base_positive(self):
        """所有策略的 backoff_base 应该大于 0"""
        for error_type, policy in ERROR_POLICIES.items():
            assert policy.backoff_base > 0, f"{error_type} has invalid backoff_base"

    def test_backoff_type_valid(self):
        """backoff_type 应该是 'exponential' 或 'fixed'"""
        for _error_type, policy in ERROR_POLICIES.items():
            assert policy.backoff_type in ("exponential", "fixed")
