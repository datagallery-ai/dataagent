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
    "BirdError",
    "SchemaNotFoundError",
    "SemanticServiceCallError",
    "SQLSecurityValidationError",
    "ThirdPartyServiceError",
]

from dataagent.core.errors import DataAgentError


class BirdError(DataAgentError):
    """Base exception for BIRD errors exposed through DataAgent boundaries."""

    message = "BIRD internal error"
    component = "bird"
    source = "internal"

    def __init__(self, message: str | None = None, *, detail: str | None = None):
        """Initialize BIRD error."""
        self.message = message or self.message
        fact = f"{self.message}: {detail}" if detail else self.message
        super().__init__(source=self.source, component=self.component, fact=fact)


class LLMOutputParseError(BirdError):
    """Raised when BIRD cannot parse a structured LLM response."""

    message = "模型输出格式解析失败"
    component = "llm"
    source = "llm"


class ThirdPartyServiceError(BirdError):
    """Base error for third-party service failures."""

    message = "三方服务调用失败"
    component = "third_party"
    source = "tool"


class SemanticServiceCallError(ThirdPartyServiceError):
    """Raised when the semantic-service call fails."""

    message = "语义服务调用失败"
    component = "semantic_service"


class SchemaNotFoundError(BirdError):
    """Raised when schema perception returns no usable database schema."""

    message = "未检索到可用的数据库 Schema"
    component = "semantic_service"


class SQLSecurityValidationError(BirdError):
    """Raised when every generated SQL candidate remains security blocked."""

    message = "生成的 SQL 未通过安全校验"
    component = "sql_security"
    source = "constraint"
