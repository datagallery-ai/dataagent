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
"""Schema validation for tool call parameters.

Provides:
1. Required parameter presence check
2. Extra parameter truncation (for tools without *args/**kwargs)
3. Type coercion with fallback strategy
"""

import types
import typing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_args, get_origin

from loguru import logger

if TYPE_CHECKING:
    from dataagent.core.managers.action_manager.schemas import ToolSchema


def _annotation_display(tp: Any) -> str:
    """Human-readable label for a type hint (handles Union / generics without __name__)."""
    name = getattr(tp, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return repr(tp)


@dataclass
class ValidationError:
    """Single validation error."""

    param_name: str
    message: str
    suggestion: str = ""


@dataclass
class ValidationWarning:
    """Warning during validation (non-blocking)."""

    param_name: str
    warning_type: str  # "extra_arg", "type_coercion"
    message: str
    original_value: Any = None
    corrected_value: Any = None


@dataclass
class ValidationResult:
    """Validation result."""

    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationWarning] = field(default_factory=list)
    corrected_args: dict[str, Any] = field(default_factory=dict)

    @property
    def formatted_message(self) -> str:
        """Get formatted error message suitable for LLM understanding."""
        if self.valid:
            parts = ["Validation passed"]
            if self.warnings:
                parts.append(f"{len(self.warnings)} warning(s):")
                for w in self.warnings:
                    parts.append(f"  - {w.message}")
            return "\n".join(parts)

        lines = ["Parameter validation failed:"]
        for i, err in enumerate(self.errors, 1):
            lines.append(f"{i}. {err.message}")
            if err.suggestion:
                lines.append(f"   Suggestion: {err.suggestion}")

        return "\n".join(lines)

    @classmethod
    def success(cls) -> "ValidationResult":
        """Create a success result."""
        return cls(valid=True)

    @classmethod
    def failure(cls, errors: list[ValidationError]) -> "ValidationResult":
        """Create a failure result."""
        return cls(valid=False, errors=errors)


class SchemaValidator:
    """Tool parameter schema validator.

    Features:
    1. Required parameter presence check
    2. Extra parameter truncation (for tools without *args/**kwargs)
    3. Type coercion with fallback strategy
    """

    # 类型转换映射：目标类型 -> 可转换的源类型列表
    TYPE_COERCIONS: dict[type, list[type]] = {
        int: [float, str],
        float: [int, str],
        str: [int, float, bool],
        bool: [str],
        list: [str],
        dict: [str],
    }

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    @staticmethod
    def _runtime_expected_types(expected_type: Any) -> tuple[type, ...] | None:
        """Return runtime-checkable types for ``isinstance``.

        Python's ``isinstance`` does not accept parameterized generics like ``list[str]``.
        This helper normalizes typing annotations into concrete runtime types.

        Returns:
            - tuple[type, ...]: types usable in isinstance
            - None: meaning "skip isinstance check" (e.g. Any/unknown typing forms)
        """
        if expected_type is None:
            return None
        if expected_type is Any:
            return None

        origin = get_origin(expected_type)
        if origin is None:
            # Covers normal runtime types (int/str/list/...) and also some typing objects.
            if isinstance(expected_type, type):
                return (expected_type,)
            return None

        # Union / Optional — MUST precede isinstance(origin, type). Both typing.Union and
        # types.UnionType are subclasses of ``type``, but they are not usable as the second
        # argument to isinstance() as "container" types for membership checks.
        if origin is typing.Union or origin is types.UnionType:
            args = get_args(expected_type)
            out: list[type] = []
            for a in args:
                rt = SchemaValidator._runtime_expected_types(a)
                if rt:
                    out.extend(rt)
            return tuple(dict.fromkeys(out)) if out else None

        # PEP 585 generics like list[str] / dict[str, int] => origin=list/dict...
        if isinstance(origin, type) and origin is not types.UnionType:
            return (origin,)

        return None

    @staticmethod
    def _truncate_extra_args(
        tool_args: dict[str, Any],
        schema_params: set[str],
    ) -> tuple[dict[str, Any], list[ValidationWarning]]:
        """截断多余参数（工具没有 *args/**kwargs 时）

        Args:
            tool_args: 原始参数
            schema_params: schema 中定义的参数名集合

        Returns:
            (截断后的参数, 警告列表)
        """
        corrected = dict(tool_args)
        warnings: list[ValidationWarning] = []
        removed_keys = []

        for key in tool_args:
            if key not in schema_params:
                removed_keys.append(key)
                del corrected[key]
                warnings.append(
                    ValidationWarning(
                        param_name=key,
                        warning_type="extra_arg",
                        message=f"Parameter '{key}' is not defined in tool schema and will be ignored",
                        original_value=tool_args[key],
                        corrected_value=None,
                    )
                )
                logger.debug(f"[SchemaValidator] Extra parameter removed: {key}")

        if removed_keys:
            logger.debug(f"[SchemaValidator] Removed {len(removed_keys)} extra parameter(s): {removed_keys}")

        return corrected, warnings

    @staticmethod
    def _coerce_type(
        param_name: str,
        value: Any,
        expected_type: type,
        is_required: bool,
        default_value: Any,
    ) -> tuple[Any, ValidationWarning | None]:
        """尝试类型转换

        Args:
            param_name: 参数名
            value: 原始值
            expected_type: 期望的 Python 类型
            is_required: 是否为必填参数
            default_value: 参数默认值

        Returns:
            (转换后的值或原值, 警告或None)
        """
        if value is None:
            return value, None

        # 类型匹配或可隐式转换（如 int -> float）
        runtime_types = SchemaValidator._runtime_expected_types(expected_type)
        if runtime_types is not None and isinstance(value, runtime_types):
            return value, None

        union_origin = get_origin(expected_type)
        if union_origin is typing.Union or union_origin is types.UnionType:
            if is_required:
                return None, ValidationWarning(
                    param_name=param_name,
                    warning_type="type_error",
                    message=(
                        f"Parameter '{param_name}' has invalid type: expected {expected_type!r}, "
                        f"got {type(value).__name__}. Value {value!r} cannot be converted."
                    ),
                    original_value=value,
                    corrected_value=None,
                )
            return default_value, ValidationWarning(
                param_name=param_name,
                warning_type="type_coercion",
                message=(
                    f"Parameter '{param_name}' has invalid type: expected {expected_type!r}, "
                    f"got {type(value).__name__}. Falling back to default: {default_value!r}"
                ),
                original_value=value,
                corrected_value=default_value,
            )

        # 尝试转换
        try:
            if expected_type is int:
                # 处理 bool -> int (True -> 1, False -> 0)
                if isinstance(value, (bool, float)):
                    new_value = int(value)
                elif isinstance(value, str):
                    # 处理字符串 "true"/"false" -> 1/0
                    if value.lower() in ("true", "yes", "1"):
                        new_value = 1
                    elif value.lower() in ("false", "no", "0"):
                        new_value = 0
                    else:
                        new_value = int(float(value))
                else:
                    raise ValueError(f"Cannot convert {type(value).__name__} to int")
            elif expected_type is float:
                if isinstance(value, bool):
                    new_value = float(int(value))
                elif isinstance(value, str):
                    new_value = float(value)
                else:
                    new_value = float(value)
            elif expected_type is str:
                new_value = str(value)
            elif expected_type is bool:
                new_value = value.lower() in ("true", "yes", "1", "on") if isinstance(value, str) else bool(value)
            elif expected_type is list:
                new_value = [value] if not isinstance(value, list) else value
            elif expected_type is dict:
                if not isinstance(value, dict):
                    raise TypeError(f"Cannot convert {type(value).__name__} to dict")
                new_value = value
            else:
                raise TypeError(f"Unsupported type: {expected_type}")

            return new_value, ValidationWarning(
                param_name=param_name,
                warning_type="type_coercion",
                message=f"Parameter '{param_name}' type coercion: {type(value).__name__} -> {expected_type.__name__}, "
                f"value: {value!r} -> {new_value!r}",
                original_value=value,
                corrected_value=new_value,
            )
        except (ValueError, TypeError):
            if is_required:
                # 必填参数类型错误：返回错误信息（不返回具体转换值）
                return None, ValidationWarning(
                    param_name=param_name,
                    warning_type="type_error",
                    message=f"Parameter '{param_name}' has invalid type: expected {expected_type.__name__}, "
                    f"got {type(value).__name__}. Value '{value}' cannot be converted.",
                    original_value=value,
                    corrected_value=None,
                )
            else:
                # 选填参数类型错误：回退到默认值
                return default_value, ValidationWarning(
                    param_name=param_name,
                    warning_type="type_coercion",
                    message=f"Parameter '{param_name}' has invalid type: expected {expected_type.__name__}, "
                    f"got {type(value).__name__}. Falling back to default: {default_value!r}",
                    original_value=value,
                    corrected_value=default_value,
                )

    def validate(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        schema: "ToolSchema | None",
    ) -> ValidationResult:
        """Validate and coerce tool parameters.

        Steps:
        1. Truncate extra arguments (not defined in schema)
        2. Check required parameters are present
        3. Coerce parameter types with fallback strategy
        """
        if not self.enabled:
            return ValidationResult.success()

        if schema is None:
            return ValidationResult.success()

        errors: list[ValidationError] = []
        warnings: list[ValidationWarning] = []
        corrected_args = dict(tool_args)

        schema_params = {p.name for p in schema.parameters}

        logger.debug(f"[SchemaValidator] Validating tool '{tool_name}' with args: {tool_args}")

        # ============================================================
        # 步骤1: 截断多余参数
        # ============================================================
        corrected_args, truncate_warnings = self._truncate_extra_args(tool_args, schema_params)
        warnings.extend(truncate_warnings)

        # ============================================================
        # 步骤2: 检查必填参数 + 类型转换
        # ============================================================
        for param in schema.parameters:
            param_name = param.name
            value = corrected_args.get(param_name)

            # 必填参数检查
            if (
                param.required
                and (param_name not in corrected_args or corrected_args[param_name] is None)
                and param.default is None
            ):
                errors.append(
                    ValidationError(
                        param_name=param_name,
                        message=f"Required parameter '{param_name}' is missing",
                        suggestion=f"Please provide a value for '{param_name}'. {param.description}",
                    )
                )
                logger.debug(f"[SchemaValidator] Missing required param: {param_name}")
                continue

            # 处理 None 值（用默认值或跳过）
            if value is None:
                continue

            # 类型检查和转换
            new_value, type_warning = self._coerce_type(
                param_name=param_name,
                value=value,
                expected_type=param.type,
                is_required=param.required,
                default_value=param.default,
            )

            if type_warning:
                if type_warning.warning_type == "type_error":
                    # 必填参数类型错误 -> 添加到错误列表
                    errors.append(
                        ValidationError(
                            param_name=param_name,
                            message=type_warning.message,
                            suggestion=(
                                f"Please provide a valid {_annotation_display(param.type)} value for '{param_name}'"
                            ),
                        )
                    )
                else:
                    # 类型转换警告或选填参数回退
                    warnings.append(type_warning)
                    if new_value is not None:
                        corrected_args[param_name] = new_value
                    elif type_warning.warning_type == "type_coercion" and type_warning.corrected_value is not None:
                        corrected_args[param_name] = type_warning.corrected_value

        if errors:
            logger.debug(
                f"[SchemaValidator] Validation failed for '{tool_name}': {len(errors)} error(s), "
                f"{len(warnings)} warning(s)"
            )
            result = ValidationResult.failure(errors)
            result.warnings = warnings
            result.corrected_args = corrected_args
            return result

        logger.debug(f"[SchemaValidator] Validation passed for '{tool_name}' ({len(warnings)} warning(s))")
        result = ValidationResult.success()
        result.warnings = warnings
        result.corrected_args = corrected_args
        return result


class ParamsValueError(Exception):
    """Parameter validation error exception."""

    def __init__(
        self,
        tool_name: str,
        tool_call_id: str,
        errors: list[ValidationError],
        message: str = "",
    ):
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.errors = errors
        self.message = message
        super().__init__(message)
