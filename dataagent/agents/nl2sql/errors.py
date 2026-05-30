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
    "MetaVisorServiceError",
    "NL2SQLError",
    "SQLServiceError",
    "ThirdPartyServiceError",
    "ValueMatchServiceError",
]

from typing import Any


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
    """Base error for third-party metadata or value-match service failures."""

    code = "NL2SQL-META-000"
    message = "三方服务调用失败"
    http_status = 502
    retryable = True
    component = "third_party"


class MetaVisorServiceError(ThirdPartyServiceError):
    """Raised when the MetaVisor service call fails."""

    code = "NL2SQL-META-001"
    message = "MetaVisor 服务调用失败"
    component = "metavisor"


class ValueMatchServiceError(ThirdPartyServiceError):
    """Raised when the ValueMatch service call fails."""

    code = "NL2SQL-META-002"
    message = "ValueMatch 服务调用失败"
    component = "valuematch"


class SQLServiceError(NL2SQLError):
    """Raised when the SQL service infrastructure is unavailable."""

    code = "NL2SQL-SQL-001"
    message = "SQL 服务调用失败"
    http_status = 502
    retryable = True
    component = "sql_service"
