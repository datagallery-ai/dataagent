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
"""Result models shared by the independent SQL security module."""

# ruff: noqa: UP045

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SecurityViolation:
    """Describe one blocking SQL security rule violation."""

    rule_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        """Return a serializable representation of the violation."""
        return {"rule_id": self.rule_id, "message": self.message}


@dataclass(frozen=True)
class SecurityCheckResult:
    """Contain all violations found for one SQL candidate."""

    violations: list[SecurityViolation] = field(default_factory=list)
    normalized_sql: Optional[str] = None

    @property
    def blocked(self) -> bool:
        """Return whether the candidate must be blocked."""
        return bool(self.violations)
