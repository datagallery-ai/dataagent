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
"""Tool argument backfill module.

Provides:
1. Default value backfill
2. Path parameter validation (no auto-conversion)
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from dataagent.core.managers.action_manager.schemas import ToolSchema


class BackfillAction(Enum):
    """Backfill action types."""

    NONE = "none"
    DEFAULT_VALUE = "default_value"


@dataclass
class BackfillChange:
    """Single backfill change record."""

    param_name: str
    action: BackfillAction
    original_value: Any = None
    new_value: Any = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "param": self.param_name,
            "action": self.action.value,
            "original": self.original_value,
            "new": self.new_value,
            "message": self.message,
        }


@dataclass
class BackfillResult:
    """Backfill result."""

    success: bool
    backfilled_args: dict[str, Any]
    changes: list[BackfillChange] = field(default_factory=list)

    @classmethod
    def success_result(
        cls, backfilled_args: dict[str, Any], changes: list[BackfillChange] | None = None
    ) -> "BackfillResult":
        """Return backfilled result."""
        return cls(success=True, backfilled_args=backfilled_args, changes=changes or [])


class ToolArgBackfiller:
    """Tool argument backfiller."""

    def __init__(
        self,
        enable_default_backfill: bool = True,
    ):
        self.enable_default_backfill = enable_default_backfill

    def backfill(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        schema: "ToolSchema | None",
    ) -> BackfillResult:
        """Execute argument backfill."""
        backfilled_args = dict(tool_args)
        changes: list[BackfillChange] = []

        if schema is None:
            logger.debug(f"[Backfiller] No schema for '{tool_name}', skipping backfill")
            return BackfillResult.success_result(backfilled_args, changes)

        schema_params = {p.name: p for p in schema.parameters}

        logger.debug(f"[Backfiller] Processing '{tool_name}' with args: {tool_args}")

        # Default value backfill
        if self.enable_default_backfill:
            for param_name, param_schema in schema_params.items():
                if (
                    param_name not in backfilled_args
                    or backfilled_args[param_name] is None
                    and param_schema.default is not None
                ):
                    backfilled_args[param_name] = param_schema.default
                    changes.append(
                        BackfillChange(
                            param_name=param_name,
                            action=BackfillAction.DEFAULT_VALUE,
                            original_value=None,
                            new_value=param_schema.default,
                            message=f"Applied default value: {param_schema.default}",
                        )
                    )
                    logger.debug(f"[Backfiller] Applied default for '{param_name}': {param_schema.default}")

        if changes:
            logger.debug(f"[Backfiller] Backfill complete for '{tool_name}': {len(changes)} change(s) applied")
        else:
            logger.debug(f"[Backfiller] No changes for '{tool_name}'")

        return BackfillResult.success_result(backfilled_args, changes)
