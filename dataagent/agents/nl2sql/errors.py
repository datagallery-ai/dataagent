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
from __future__ import annotations

__all__ = [
    "LLMOutputParseError",
    "NL2SQLError",
    "SemanticServiceCallError",
    "SQLSecurityValidationError",
    "SQLServiceError",
    "ThirdPartyServiceError",
]

from typing import Any

_SQL_SECURITY_PUBLIC_CODES = {
    "SQL-001": "NL2SQL-SEC-002",
    "SQL-002": "NL2SQL-SEC-003",
    "FUNCTION-001": "NL2SQL-SEC-004",
    "SYNTAX-001": "NL2SQL-SEC-005",
    "RESOURCE-001": "NL2SQL-SEC-006",
    "RESOURCE-002": "NL2SQL-SEC-007",
    "RESOURCE-003": "NL2SQL-SEC-008",
    "RESOURCE-006": "NL2SQL-SEC-009",
    "RESOURCE-007": "NL2SQL-SEC-010",
    "RESOURCE-008": "NL2SQL-SEC-011",
    "RESOURCE-009": "NL2SQL-SEC-012",
    "SCHEMA-001": "NL2SQL-SEC-013",
    "SCHEMA-002": "NL2SQL-SEC-014",
    "SCHEMA-003": "NL2SQL-SEC-015",
    "SCHEMA-004": "NL2SQL-SEC-016",
}


def _map_sql_security_violations(violations: list[dict[str, str]]) -> list[dict[str, str]]:
    """Convert internal security rule identifiers to public API errors."""
    public_errors: list[dict[str, str]] = []
    seen = set()
    for violation in violations:
        rule_id = violation.get("rule_id", "")
        message = violation.get("message", "")
        if not message:
            continue
        code = _SQL_SECURITY_PUBLIC_CODES.get(rule_id, "NL2SQL-SEC-001")
        key = (code, message)
        if key in seen:
            continue
        seen.add(key)
        public_errors.append({"code": code, "message": message})
    return public_errors


class NL2SQLError(Exception):
    """Base exception for NL2SQL errors that should be translated at service boundaries."""

    code = "NL2SQL-INT-001"
    message = "NL2SQL internal error"
    http_status = 500
    retryable = False
    component = "nl2sql"

    def __init__(self, message: str | None = None, *, detail: str | None = None):
        """Initialize NL2SQL error."""
        self.message = message or self.message
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Return a stable payload for API wrappers."""
        return {
            "success": False,
            "code": self.code,
            "message": self.message,
            "http_status": self.http_status,
            "component": self.component,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class LLMOutputParseError(NL2SQLError):
    """Raised when NL2SQL cannot parse a structured LLM response."""

    code = "NL2SQL-LLM-002"
    message = "模型输出格式解析失败"
    http_status = 502
    retryable = True
    component = "llm"


class ThirdPartyServiceError(NL2SQLError):
    """Base error for third-party service failures."""

    code = "NL2SQL-META-000"
    message = "三方服务调用失败"
    http_status = 502
    retryable = True
    component = "third_party"


class SemanticServiceCallError(ThirdPartyServiceError):
    """Raised when the semantic-service call fails."""

    code = "NL2SQL-META-001"
    message = "语义服务调用失败"
    component = "semantic_service"


class SQLServiceError(NL2SQLError):
    """Raised when the SQL service infrastructure is unavailable."""

    code = "NL2SQL-SQL-001"
    message = "SQL 服务调用失败"
    http_status = 502
    retryable = True
    component = "sql_service"


class SQLSecurityValidationError(NL2SQLError):
    """Raised when every generated SQL candidate remains security blocked."""

    code = "NL2SQL-SEC-001"
    message = "生成的 SQL 未通过安全校验"
    http_status = 422
    retryable = False
    component = "sql_security"

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: str | None = None,
        violations: list[dict[str, str]] | None = None,
    ):
        """Initialize a security error with public error codes and messages."""
        self.errors = _map_sql_security_violations(violations or [])
        if len(self.errors) == 1:
            self.code = self.errors[0].get("code", self.code)
            message = self.errors[0].get("message", message)
        super().__init__(message, detail=detail)

    def to_dict(self) -> dict[str, Any]:
        """Return the public SQL security error payload."""
        payload = super().to_dict()
        if self.errors:
            payload["errors"] = self.errors
        return payload
