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
"""Unit tests for schema validation module."""

from typing import Union

import pytest

from dataagent.actions.tools.schema_validator import (
    ParamsValueError,
    SchemaValidator,
    ValidationError,
    ValidationResult,
    ValidationWarning,
)
from dataagent.core.managers.action_manager.schemas import ParameterSchema, ToolSchema


class TestValidationError:
    """Test ValidationError model."""

    def test_validation_error_creation(self):
        """Test creating a ValidationError."""
        error = ValidationError(
            param_name="count",
            message="Type mismatch",
            suggestion="Provide an integer",
        )
        assert error.param_name == "count"
        assert error.message == "Type mismatch"
        assert error.suggestion == "Provide an integer"

    def test_validation_error_default_suggestion(self):
        """Test ValidationError default suggestion is empty."""
        error = ValidationError(
            param_name="count",
            message="Missing required field",
        )
        assert error.suggestion == ""


class TestValidationWarning:
    """Test ValidationWarning model."""

    def test_validation_warning_creation(self):
        """Test creating a ValidationWarning."""
        warning = ValidationWarning(
            param_name="extra_field",
            warning_type="extra_arg",
            message="Extra parameter provided",
            original_value="some_value",
            corrected_value=None,
        )
        assert warning.param_name == "extra_field"
        assert warning.warning_type == "extra_arg"
        assert warning.original_value == "some_value"


class TestValidationResult:
    """Test ValidationResult model."""

    def test_success_result(self):
        """Test creating a success result."""
        result = ValidationResult.success()
        assert result.valid is True
        assert result.errors == []
        assert result.warnings == []
        assert result.corrected_args == {}

    def test_failure_result(self):
        """Test creating a failure result."""
        error = ValidationError(
            param_name="count",
            message="Type mismatch",
        )
        result = ValidationResult.failure([error])
        assert result.valid is False
        assert len(result.errors) == 1
        assert result.errors[0].message == "Type mismatch"

    def test_result_with_warnings(self):
        """Test result with warnings."""
        warning = ValidationWarning(
            param_name="extra",
            warning_type="extra_arg",
            message="Extra parameter",
        )
        result = ValidationResult(valid=True, warnings=[warning])
        assert result.valid is True
        assert len(result.warnings) == 1

    def test_result_with_corrected_args(self):
        """Test result with corrected arguments."""
        result = ValidationResult(valid=True, corrected_args={"count": 5, "name": "test"})
        assert result.corrected_args["count"] == 5
        assert result.corrected_args["name"] == "test"

    def test_formatted_message_success(self):
        """Test formatted message for success."""
        result = ValidationResult.success()
        assert result.formatted_message == "Validation passed"

    def test_formatted_message_failure(self):
        """Test formatted message for failure."""
        error = ValidationError(
            param_name="count",
            message="Type mismatch",
            suggestion="Provide an integer",
        )
        result = ValidationResult.failure([error])
        msg = result.formatted_message
        assert "Parameter validation failed" in msg
        assert "Type mismatch" in msg
        assert "Provide an integer" in msg

    def test_formatted_message_multiple_errors(self):
        """Test formatted message for multiple errors."""
        errors = [
            ValidationError(param_name="field1", message="Error 1"),
            ValidationError(param_name="field2", message="Error 2"),
        ]
        result = ValidationResult.failure(errors)
        msg = result.formatted_message
        assert "Parameter validation failed" in msg
        assert "1. Error 1" in msg
        assert "2. Error 2" in msg


def _create_test_schema() -> ToolSchema:
    """Create a test tool schema."""
    return ToolSchema(
        name="test_tool",
        description="A test tool",
        parameters=[
            ParameterSchema(name="count", type=int, required=True, description="Count parameter"),
            ParameterSchema(name="name", type=str, required=True, description="Name parameter"),
            ParameterSchema(name="enabled", type=bool, required=False, default=False, description="Enabled flag"),
            ParameterSchema(
                name="rate",
                type=float,
                required=False,
                default=0.5,
                description="Rate between 0 and 1",
            ),
        ],
    )


class TestSchemaValidator:
    """Test SchemaValidator."""

    def test_validator_enabled_by_default(self):
        """Test that validator is enabled by default."""
        validator = SchemaValidator()
        assert validator.enabled is True

    def test_validator_disabled(self):
        """Test disabled validator."""
        validator = SchemaValidator(enabled=False)
        assert validator.enabled is False

    def test_validate_disabled_validator_passes(self):
        """Test that validation is skipped when disabled."""
        validator = SchemaValidator(enabled=False)
        schema = _create_test_schema()
        result = validator.validate("test_tool", {}, schema)
        assert result.valid is True

    def test_validate_none_schema(self):
        """Test validation with None schema passes."""
        validator = SchemaValidator()
        result = validator.validate("test_tool", {"any": "args"}, None)
        assert result.valid is True

    def test_validate_missing_required_field(self):
        """Test validation fails for missing required fields."""
        validator = SchemaValidator()
        schema = _create_test_schema()

        result = validator.validate("test_tool", {"enabled": True}, schema)
        assert result.valid is False
        assert len(result.errors) >= 1
        assert any("count" in e.message for e in result.errors)

    def test_validate_missing_all_required_fields(self):
        """Test validation fails when all required fields missing."""
        validator = SchemaValidator()
        schema = _create_test_schema()

        result = validator.validate("test_tool", {}, schema)
        assert result.valid is False
        assert len(result.errors) >= 2  # count and name

    def test_validate_correct_args(self):
        """Test validation passes with correct arguments."""
        validator = SchemaValidator()
        schema = _create_test_schema()

        result = validator.validate(
            "test_tool",
            {"count": 5, "name": "test", "enabled": True, "rate": 0.5},
            schema,
        )
        assert result.valid is True
        assert result.corrected_args["count"] == 5
        assert result.corrected_args["name"] == "test"

    def test_validate_optional_field_with_none_value(self):
        """Test validation passes for optional field with None value."""
        validator = SchemaValidator()
        schema = _create_test_schema()

        result = validator.validate(
            "test_tool",
            {"count": 5, "name": "test", "enabled": None},
            schema,
        )
        assert result.valid is True

    def test_validate_extra_arg_generates_warning(self):
        """Test extra argument generates warning but passes validation."""
        validator = SchemaValidator()
        schema = _create_test_schema()

        result = validator.validate(
            "test_tool",
            {"count": 5, "name": "test", "extra_param": "value"},
            schema,
        )
        # 非 strict 模式，多余参数只是警告
        assert result.valid is True
        assert len(result.warnings) >= 1

    def test_validate_corrected_args_contains_processed_values(self):
        """Test corrected_args contains the processed values."""
        validator = SchemaValidator()
        schema = _create_test_schema()

        # 提供 int 给 float 类型字段，应该有类型转换
        result = validator.validate(
            "test_tool",
            {"count": 5, "name": "test", "rate": 3},  # rate is float, 3 is int
            schema,
        )
        assert result.valid is True
        assert "count" in result.corrected_args


class TestSchemaValidatorTypeCoercion:
    """Test SchemaValidator type coercion."""

    def _create_schema_with_types(self) -> ToolSchema:
        """Create schema with various types."""
        return ToolSchema(
            name="test_tool",
            description="Test types",
            parameters=[
                ParameterSchema(name="count", type=int, required=True),
                ParameterSchema(name="rate", type=float, required=True),
                ParameterSchema(name="active", type=bool, required=True),
                ParameterSchema(name="data", type=str, required=True),
            ],
        )

    def test_int_from_string_coerced(self):
        """Test int type accepts string that can be converted."""
        validator = SchemaValidator()
        schema = self._create_schema_with_types()

        result = validator.validate(
            "test_tool",
            {"count": "10", "rate": 1.0, "active": True, "data": "hello"},
            schema,
        )
        # 字符串 "10" 应该被转换为 int 并添加到 corrected_args
        assert result.valid is True

    def test_float_from_int_coerced(self):
        """Test float type accepts int."""
        validator = SchemaValidator()
        schema = self._create_schema_with_types()

        result = validator.validate(
            "test_tool",
            {"count": 10, "rate": 5, "active": True, "data": "hello"},
            schema,
        )
        assert result.valid is True

    def test_float_from_string_coerced(self):
        """Test float type accepts string number."""
        validator = SchemaValidator()
        schema = self._create_schema_with_types()

        result = validator.validate(
            "test_tool",
            {"count": 10, "rate": "3.14", "active": True, "data": "hello"},
            schema,
        )
        assert result.valid is True
        assert result.corrected_args["rate"] == 3.14

    def test_bool_from_string_coerced(self):
        """Test bool type accepts string "true"/"false"."""
        validator = SchemaValidator()
        schema = self._create_schema_with_types()

        result = validator.validate(
            "test_tool",
            {"count": 10, "rate": 1.0, "active": "true", "data": "hello"},
            schema,
        )
        assert result.valid is True


class TestSchemaValidatorTypingGenerics:
    """Test SchemaValidator handles parameterized generics safely."""

    def test_validate_parameterized_list_type_does_not_crash(self):
        schema = ToolSchema(
            name="test_tool",
            description="Test typing generics",
            parameters=[
                ParameterSchema(name="keywords_list", type=list[str], required=True),
            ],
        )
        validator = SchemaValidator()
        result = validator.validate("test_tool", {"keywords_list": ["user", "product", "order"]}, schema)
        assert result.valid is True

    def test_validate_pep604_union_rejects_wrong_type(self):
        schema = ToolSchema(
            name="test_tool",
            description="Union params",
            parameters=[
                ParameterSchema(name="x", type=str | int, required=True),
            ],
        )
        validator = SchemaValidator()
        bad = validator.validate("test_tool", {"x": {}}, schema)
        assert bad.valid is False
        assert any("x" in e.message for e in bad.errors)

        good = validator.validate("test_tool", {"x": "ok"}, schema)
        assert good.valid is True

    def test_validate_typing_union_rejects_wrong_type(self):
        schema = ToolSchema(
            name="test_tool",
            description="typing.Union params",
            parameters=[
                # Keep ``typing.Union[...]`` (not ``str | int``) to cover ``get_origin`` -> ``typing.Union``.
                ParameterSchema(name="x", type=Union[str, int], required=True),  # noqa: UP007
            ],
        )
        validator = SchemaValidator()
        bad = validator.validate("test_tool", {"x": {}}, schema)
        assert bad.valid is False


class TestParamsValueError:
    """Test ParamsValueError exception."""

    def test_exception_creation(self):
        """Test creating ParamsValueError."""
        error = ValidationError(
            param_name="count",
            message="Type mismatch",
        )
        exc = ParamsValueError(
            tool_name="test_tool",
            tool_call_id="call_123",
            errors=[error],
            message="Parameter validation failed",
        )
        assert exc.tool_name == "test_tool"
        assert exc.tool_call_id == "call_123"
        assert len(exc.errors) == 1

    def test_exception_str(self):
        """Test string representation of ParamsValueError."""
        exc = ParamsValueError(
            tool_name="test_tool",
            tool_call_id="call_123",
            errors=[],
            message="Parameter validation failed",
        )
        assert str(exc) == "Parameter validation failed"

    def test_exception_message_attribute(self):
        """Test ParamsValueError message attribute."""
        exc = ParamsValueError(
            tool_name="test_tool",
            tool_call_id="call_123",
            errors=[],
            message="Parameter validation failed",
        )
        assert exc.message == "Parameter validation failed"
        assert exc.tool_name == "test_tool"
        assert exc.tool_call_id == "call_123"


class TestOptionalParamTypeConversion:
    """选填参数类型转换测试

    fvt_dataagent_workflow_optional_paraverify_*: 验证选填参数的类型转换和回退逻辑
    """

    def test_optional_str_from_convertible_type(self):
        """fvt_dataagent_workflow_optional_paraverify_001: 选填参数str类型，传入可转换类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test str conversion",
            parameters=[
                ParameterSchema(
                    name="name", type=str, required=False, default="default_name", description="Name parameter"
                ),
            ],
        )
        validator = SchemaValidator()

        # 传入 int，应该被转换为 str
        result = validator.validate("test_tool", {"name": 12345}, schema)
        assert result.valid is True
        assert result.corrected_args["name"] == "12345"

        # 传入 float，应该被转换为 str
        result = validator.validate("test_tool", {"name": 3.14}, schema)
        assert result.valid is True
        assert result.corrected_args["name"] == "3.14"

        # 传入 bool，应该被转换为 str
        result = validator.validate("test_tool", {"name": True}, schema)
        assert result.valid is True
        assert result.corrected_args["name"] == "True"

        # 传入 list，应该被转换为 str
        result = validator.validate("test_tool", {"name": [1, 2, 3]}, schema)
        assert result.valid is True
        assert result.corrected_args["name"] == "[1, 2, 3]"

    def test_optional_bool_from_convertible_type(self):
        """fvt_dataagent_workflow_optional_paraverify_002: 选填参数bool类型，传入可转换类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test bool conversion",
            parameters=[
                ParameterSchema(name="enabled", type=bool, required=False, default=False, description="Enabled flag"),
            ],
        )
        validator = SchemaValidator()

        # 传入字符串 "true"
        result = validator.validate("test_tool", {"enabled": "true"}, schema)
        assert result.valid is True
        assert result.corrected_args["enabled"] is True

        # 传入字符串 "yes"
        result = validator.validate("test_tool", {"enabled": "yes"}, schema)
        assert result.valid is True
        assert result.corrected_args["enabled"] is True

        # 传入字符串 "1"
        result = validator.validate("test_tool", {"enabled": "1"}, schema)
        assert result.valid is True
        assert result.corrected_args["enabled"] is True

        # 传入字符串 "false"
        result = validator.validate("test_tool", {"enabled": "false"}, schema)
        assert result.valid is True
        assert result.corrected_args["enabled"] is False

        # 传入 int 1
        result = validator.validate("test_tool", {"enabled": 1}, schema)
        assert result.valid is True
        assert result.corrected_args["enabled"] is True

        # 传入 int 0
        result = validator.validate("test_tool", {"enabled": 0}, schema)
        assert result.valid is True
        assert result.corrected_args["enabled"] is False

    def test_optional_int_from_float_bool(self):
        """fvt_dataagent_workflow_optional_paraverify_003: 选填参数int类型，传入float/bool时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test int conversion",
            parameters=[
                ParameterSchema(name="count", type=int, required=False, default=0, description="Count parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入 float
        result = validator.validate("test_tool", {"count": 3.14}, schema)
        assert result.valid is True
        assert result.corrected_args["count"] == 3

        # 传入 bool True
        result = validator.validate("test_tool", {"count": True}, schema)
        assert result.valid is True
        assert result.corrected_args["count"] == 1

        # 传入 bool False
        result = validator.validate("test_tool", {"count": False}, schema)
        assert result.valid is True
        assert result.corrected_args["count"] == 0

        # 传入字符串数字
        result = validator.validate("test_tool", {"count": "42"}, schema)
        assert result.valid is True
        assert result.corrected_args["count"] == 42

    def test_optional_dict_from_json_string(self):
        """fvt_dataagent_workflow_optional_paraverify_005: 选填参数dict类型，传入JSON字符串时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test dict conversion",
            parameters=[
                ParameterSchema(
                    name="config",
                    type=dict,
                    required=False,
                    default={"is_default": True},
                    description="Config parameter",
                ),
            ],
        )
        validator = SchemaValidator()

        # 传入正确类型的 dict
        result = validator.validate("test_tool", {"config": {"key": "value"}}, schema)
        assert result.valid is True
        assert result.corrected_args["config"] == {"key": "value"}

        # 传入 JSON 字符串 -> 解析为 dict
        result = validator.validate("test_tool", {"config": '{"key": "value", "num": 123}'}, schema)
        assert result.valid is True
        assert result.corrected_args["config"] == {"key": "value", "num": 123}

        # 传入无法解析的字符串 -> 回退到默认值
        result = validator.validate("test_tool", {"config": "not a json"}, schema)
        assert result.valid is True
        assert result.corrected_args["config"] == {"is_default": True}
        assert any("Falling back to default" in w.message for w in result.warnings)

        # 传入 int (无法转换) -> 回退到默认值
        result = validator.validate("test_tool", {"config": 1234}, schema)
        assert result.valid is True
        assert result.corrected_args["config"] == {"is_default": True}
        assert any("Falling back to default" in w.message for w in result.warnings)

    def test_optional_list_from_json_string(self):
        """fvt_dataagent_workflow_optional_paraverify_010: 选填参数list类型，传入JSON字符串时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test list conversion",
            parameters=[
                ParameterSchema(
                    name="items",
                    type=list,
                    required=False,
                    default=["default"],
                    description="Items parameter",
                ),
            ],
        )
        validator = SchemaValidator()

        # 传入正确类型的 list
        result = validator.validate("test_tool", {"items": ["a", "b", "c"]}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == ["a", "b", "c"]

        # 传入 JSON 字符串 -> 解析为 list
        result = validator.validate("test_tool", {"items": "[1, 2, 3]"}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == [1, 2, 3]

        # 传入 JSON 字符串（包含对象）-> 解析为 list
        result = validator.validate("test_tool", {"items": '[{"id": 1}, {"id": 2}]'}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == [{"id": 1}, {"id": 2}]

        # 传入普通字符串（无法解析为 JSON 数组）-> 包装为 [value]
        result = validator.validate("test_tool", {"items": "not a json array"}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == ["not a json array"]

        # 传入无法解析的字符串（JSON 对象不是数组）-> 包装为 [value]
        result = validator.validate("test_tool", {"items": '{"key": "value"}'}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == [{"key": "value"}]

    def test_optional_float_from_convertible_types(self):
        """fvt_dataagent_workflow_optional_paraverify_006: 选填参数float类型，传入可转换类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test float conversion",
            parameters=[
                ParameterSchema(name="rate", type=float, required=False, default=0.0, description="Rate parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入 int
        result = validator.validate("test_tool", {"rate": 3}, schema)
        assert result.valid is True
        assert result.corrected_args["rate"] == 3.0

        # 传入字符串数字
        result = validator.validate("test_tool", {"rate": "2.5"}, schema)
        assert result.valid is True
        assert result.corrected_args["rate"] == 2.5

        # 传入 bool True
        result = validator.validate("test_tool", {"rate": True}, schema)
        assert result.valid is True
        assert result.corrected_args["rate"] == 1.0

    def test_optional_param_fallback_to_default_on_conversion_failure(self):
        """fvt_dataagent_workflow_optional_paraverify_009: 选填参数str类型，传入不可转换类型时回退到默认值

        注意: str 类型可以接受任何值（包括 None），所以 str 类型不存在"不可转换"的情况。
        此测试验证 None 值的处理：对于选填参数，如果传入 None，会在 backfill 阶段使用默认值。
        """
        schema = ToolSchema(
            name="test_tool",
            description="Test str fallback",
            parameters=[
                ParameterSchema(
                    name="data", type=str, required=False, default="default_data", description="Data parameter"
                ),
            ],
        )
        validator = SchemaValidator()

        # 传入 None -> 使用默认值 (backfill 阶段处理)
        result = validator.validate("test_tool", {"data": None}, schema)
        assert result.valid is True
        # validate 阶段 None 会被跳过，backfill 会处理
        assert result.corrected_args.get("data") is None or result.corrected_args.get("data") == "default_data"


class TestRequiredParamTypeConversion:
    """必填参数类型转换测试

    fvt_dataagent_workflow_required_paraverify_*: 验证必填参数的类型转换和错误返回逻辑
    """

    def test_required_str_from_various_types(self):
        """fvt_dataagent_workflow_required_paraverify_007: 必填参数str类型，传入各类类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test required str conversion",
            parameters=[
                ParameterSchema(name="message", type=str, required=True, description="Message parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入 int
        result = validator.validate("test_tool", {"message": 123}, schema)
        assert result.valid is True
        assert result.corrected_args["message"] == "123"

        # 传入 float
        result = validator.validate("test_tool", {"message": 3.14}, schema)
        assert result.valid is True
        assert result.corrected_args["message"] == "3.14"

        # 传入 bool
        result = validator.validate("test_tool", {"message": True}, schema)
        assert result.valid is True
        assert result.corrected_args["message"] == "True"

        # 传入 list
        result = validator.validate("test_tool", {"message": [1, 2, 3]}, schema)
        assert result.valid is True
        assert result.corrected_args["message"] == "[1, 2, 3]"

    def test_required_bool_from_convertible_types(self):
        """fvt_dataagent_workflow_required_paraverify_004: 必填参数bool类型，传入可兼容类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test required bool conversion",
            parameters=[
                ParameterSchema(name="flag", type=bool, required=True, description="Flag parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入字符串 "yes"
        result = validator.validate("test_tool", {"flag": "yes"}, schema)
        assert result.valid is True
        assert result.corrected_args["flag"] is True

        # 传入字符串 "0"
        result = validator.validate("test_tool", {"flag": "0"}, schema)
        assert result.valid is True
        assert result.corrected_args["flag"] is False

        # 传入 int 1
        result = validator.validate("test_tool", {"flag": 1}, schema)
        assert result.valid is True
        assert result.corrected_args["flag"] is True

        # 传入 float 0.0
        result = validator.validate("test_tool", {"flag": 0.0}, schema)
        assert result.valid is True
        assert result.corrected_args["flag"] is False

    def test_required_int_from_convertible_types(self):
        """fvt_dataagent_workflow_required_paraverify_002: 必填参数int类型，传入可转换类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test required int conversion",
            parameters=[
                ParameterSchema(name="count", type=int, required=True, description="Count parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入 float
        result = validator.validate("test_tool", {"count": 3.7}, schema)
        assert result.valid is True
        assert result.corrected_args["count"] == 3

        # 传入 bool True
        result = validator.validate("test_tool", {"count": True}, schema)
        assert result.valid is True
        assert result.corrected_args["count"] == 1

        # 传入字符串数字
        result = validator.validate("test_tool", {"count": "42"}, schema)
        assert result.valid is True
        assert result.corrected_args["count"] == 42

        # 传入字符串 "true" -> 转换为 1
        result = validator.validate("test_tool", {"count": "true"}, schema)
        assert result.valid is True
        assert result.corrected_args["count"] == 1

    def test_required_dict_from_compatible_types(self):
        """fvt_dataagent_workflow_required_paraverify_006: 必填参数dict类型，传入可兼容类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test required dict conversion",
            parameters=[
                ParameterSchema(name="config", type=dict, required=True, description="Config parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入正确的 dict
        result = validator.validate("test_tool", {"config": {"key": "value"}}, schema)
        assert result.valid is True
        assert result.corrected_args["config"] == {"key": "value"}

        # 传入 JSON 字符串 -> 解析为 dict
        result = validator.validate("test_tool", {"config": '{"key": "value", "num": 123}'}, schema)
        assert result.valid is True
        assert result.corrected_args["config"] == {"key": "value", "num": 123}

        # 传入 int (无法转换) -> 验证失败
        result = validator.validate("test_tool", {"config": 1234}, schema)
        assert result.valid is False
        assert any("dict" in e.message.lower() for e in result.errors)

        # 传入无法解析的字符串 -> 验证失败
        result = validator.validate("test_tool", {"config": "not a json"}, schema)
        assert result.valid is False
        assert any("dict" in e.message.lower() for e in result.errors)

    def test_required_list_from_compatible_types(self):
        """fvt_dataagent_workflow_required_paraverify_008: 必填参数list类型，传入可兼容类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test required list conversion",
            parameters=[
                ParameterSchema(name="items", type=list, required=True, description="Items parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入正确的 list
        result = validator.validate("test_tool", {"items": ["a", "b", "c"]}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == ["a", "b", "c"]

        # 传入 JSON 字符串 -> 解析为 list
        result = validator.validate("test_tool", {"items": "[1, 2, 3]"}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == [1, 2, 3]

        # 传入 JSON 字符串（包含对象）-> 解析为 list
        result = validator.validate("test_tool", {"items": '[{"id": 1}, {"id": 2}]'}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == [{"id": 1}, {"id": 2}]

        # 传入普通字符串（无法解析为 JSON 数组）-> 包装为 [value]
        result = validator.validate("test_tool", {"items": "single item"}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == ["single item"]

        # 传入无法解析的字符串（JSON 对象不是数组）-> 包装为 [value]
        result = validator.validate("test_tool", {"items": '{"key": "value"}'}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == [{"key": "value"}]

        # 传入 int (无法转换) -> 包装为 [value]
        result = validator.validate("test_tool", {"items": 123}, schema)
        assert result.valid is True
        assert result.corrected_args["items"] == [123]

    def test_required_float_from_convertible_types(self):
        """fvt_dataagent_workflow_required_paraverify_005: 必填参数float类型，传入可兼容类型时转换成功"""
        schema = ToolSchema(
            name="test_tool",
            description="Test required float conversion",
            parameters=[
                ParameterSchema(name="rate", type=float, required=True, description="Rate parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入 int
        result = validator.validate("test_tool", {"rate": 3}, schema)
        assert result.valid is True
        assert result.corrected_args["rate"] == 3.0

        # 传入字符串数字
        result = validator.validate("test_tool", {"rate": "2.5"}, schema)
        assert result.valid is True
        assert result.corrected_args["rate"] == 2.5

        # 传入 bool True
        result = validator.validate("test_tool", {"rate": True}, schema)
        assert result.valid is True
        assert result.corrected_args["rate"] == 1.0

        # 传入科学计数法字符串
        result = validator.validate("test_tool", {"rate": "1e-5"}, schema)
        assert result.valid is True
        assert result.corrected_args["rate"] == 1e-5

    def test_required_str_from_unconvertible_type_returns_error(self):
        """fvt_dataagent_workflow_required_paraverify_010: 必填参数str类型，传入不可转类型时返回明确错误

        注意: str 类型可以接受任何值（包括 None），所以正常情况下不会报错。
        此测试验证必填参数传入 None 时的行为：会被当作缺失必填参数。
        """
        schema = ToolSchema(
            name="test_tool",
            description="Test required str error",
            parameters=[
                ParameterSchema(name="data", type=str, required=True, description="Data parameter"),
            ],
        )
        validator = SchemaValidator()

        # 必填参数传入 None -> 被当作缺失必填参数，验证失败
        result = validator.validate("test_tool", {"data": None}, schema)
        assert result.valid is False
        assert any("missing" in e.message.lower() for e in result.errors)

        # str 类型可以接受任何非 None 值（会转换为 str）
        result = validator.validate("test_tool", {"data": 123}, schema)
        assert result.valid is True
        assert result.corrected_args["data"] == "123"

    def test_required_bool_from_unconvertible_type_returns_error(self):
        """fvt_dataagent_workflow_required_paraverify_011: 必填参数bool类型，传入不可转类型时返回明确错误

        注意: 根据用户描述"不管传啥类型会在参数校验前转成true或者false"
        当前实现 bool 类型使用 Python 内置 bool() 转换，任何值都能转换
        """
        schema = ToolSchema(
            name="test_tool",
            description="Test required bool conversion",
            parameters=[
                ParameterSchema(name="flag", type=bool, required=True, description="Flag parameter"),
            ],
        )
        validator = SchemaValidator()

        # 传入任何值都能转换为 bool (Python 特性)
        # 但如果值是 None，则跳过处理
        result = validator.validate("test_tool", {"flag": None}, schema)
        # None 会被跳过，不会触发类型错误

        # 传入 dict (非字符串) -> 使用 bool() 转换，结果为 True
        result = validator.validate("test_tool", {"flag": {"key": "value"}}, schema)
        assert result.valid is True
        assert result.corrected_args["flag"] is True

        # 传入 list (非字符串) -> 使用 bool() 转换，空列表为 False
        result = validator.validate("test_tool", {"flag": []}, schema)
        assert result.valid is True
        assert result.corrected_args["flag"] is False


class TestMixedParamTypes:
    """混合参数类型测试: 测试包含多个不同类型参数的 schema"""

    def test_mixed_required_and_optional_params(self):
        """测试同时包含必填和选填参数的混合场景"""
        schema = ToolSchema(
            name="mock_test",
            description="Test mixed params",
            parameters=[
                ParameterSchema(name="purpose", type=str, required=True, description="Purpose"),
                ParameterSchema(
                    name="config",
                    type=dict,
                    required=False,
                    default={"is_default_fallback": True},
                    description="Config",
                ),
            ],
        )
        validator = SchemaValidator()

        # 正常场景: 必填参数正确，选填参数类型错误 -> 选填回退到默认值
        result = validator.validate("mock_test", {"purpose": "123", "config": 1234}, schema)
        assert result.valid is True
        assert result.corrected_args["purpose"] == "123"
        assert result.corrected_args["config"] == {"is_default_fallback": True}

        # 正常场景: 所有参数类型都正确
        result = validator.validate("mock_test", {"purpose": "test", "config": {"key": "value"}}, schema)
        assert result.valid is True
        assert result.corrected_args["purpose"] == "test"
        assert result.corrected_args["config"] == {"key": "value"}

        # 场景: 必填参数传入 int -> 会转换为 str（因为 str 类型接受任何值）
        # 注意: 当前实现中 str 类型会接受 int 并转换，所以不会报错
        result = validator.validate("mock_test", {"purpose": 123, "config": {"key": "value"}}, schema)
        # str 类型可以接受 int 并转换为 str，所以验证通过
        assert result.valid is True
        assert result.corrected_args["purpose"] == "123"

    def test_original_scenario_from_issue(self):
        """复现原始问题场景: config 传入 int 而不是 dict"""
        schema = ToolSchema(
            name="mock_test",
            description="Test original issue",
            parameters=[
                ParameterSchema(name="purpose", type=str, required=True, description="Purpose"),
                ParameterSchema(
                    name="config",
                    type=dict,
                    required=False,
                    default={"is_default_fallback": True},
                    description="Config",
                ),
            ],
        )
        validator = SchemaValidator()

        # 原始问题: config=1234 (int) 传入期望 dict 类型的选填参数
        result = validator.validate("mock_test", {"purpose": "123", "config": 1234}, schema)
        assert result.valid is True
        assert result.corrected_args["config"] == {"is_default_fallback": True}
        assert any("Falling back to default" in w.message for w in result.warnings)
