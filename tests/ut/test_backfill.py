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
"""Unit tests for backfill module."""

import pytest
from dataagent.core.managers.action_manager.schemas import ParameterSchema, ToolSchema

from dataagent.actions.tools.backfill import (
    BackfillAction,
    BackfillChange,
    BackfillResult,
    ToolArgBackfiller,
)


class TestBackfillAction:
    """Test BackfillAction enum."""

    def test_backfill_action_values(self):
        """Test BackfillAction enum values."""
        assert BackfillAction.NONE.value == "none"
        assert BackfillAction.DEFAULT_VALUE.value == "default_value"


class TestBackfillChange:
    """Test BackfillChange dataclass."""

    def test_backfill_change_creation(self):
        """Test creating BackfillChange."""
        change = BackfillChange(
            param_name="count",
            action=BackfillAction.DEFAULT_VALUE,
            original_value=None,
            new_value=10,
            message="Applied default value",
        )
        assert change.param_name == "count"
        assert change.action == BackfillAction.DEFAULT_VALUE
        assert change.original_value is None
        assert change.new_value == 10

    def test_backfill_change_to_dict(self):
        """Test BackfillChange.to_dict()."""
        change = BackfillChange(
            param_name="count",
            action=BackfillAction.DEFAULT_VALUE,
            original_value=None,
            new_value=10,
            message="Applied default value",
        )
        d = change.to_dict()
        assert d["param"] == "count"
        assert d["action"] == "default_value"
        assert d["original"] is None
        assert d["new"] == 10
        assert d["message"] == "Applied default value"


class TestBackfillResult:
    """Test BackfillResult dataclass."""

    def test_backfill_result_success(self):
        """Test creating successful BackfillResult."""
        result = BackfillResult.success_result({"count": 10}, [])
        assert result.success is True
        assert result.backfilled_args == {"count": 10}
        assert result.changes == []

    def test_backfill_result_with_changes(self):
        """Test BackfillResult with changes."""
        change = BackfillChange(
            param_name="count",
            action=BackfillAction.DEFAULT_VALUE,
            original_value=None,
            new_value=10,
        )
        result = BackfillResult.success_result({"count": 10}, [change])
        assert result.success is True
        assert len(result.changes) == 1

    def test_backfill_result_none_changes(self):
        """Test BackfillResult with None changes defaults to empty list."""
        result = BackfillResult.success_result({"count": 10}, None)
        assert result.changes == []


def _create_test_schema() -> ToolSchema:
    """Create a test tool schema."""
    return ToolSchema(
        name="test_tool",
        description="A test tool",
        parameters=[
            ParameterSchema(name="count", type=int, required=False, default=10),
            ParameterSchema(name="name", type=str, required=True),
        ],
    )


class TestToolArgBackfiller:
    """Test ToolArgBackfiller class."""

    def test_backfill_none_schema(self):
        """Test backfill with None schema."""
        backfiller = ToolArgBackfiller()
        result = backfiller.backfill("test_tool", {"name": "test"}, None)
        assert result.success is True
        assert result.backfilled_args == {"name": "test"}
        assert result.changes == []

    def test_backfill_default_values(self):
        """Test backfill applies default values."""
        backfiller = ToolArgBackfiller()
        schema = _create_test_schema()

        result = backfiller.backfill("test_tool", {"name": "test"}, schema)
        assert result.success is True
        assert result.backfilled_args["count"] == 10
        assert result.backfilled_args["name"] == "test"
        assert len(result.changes) == 1
        assert result.changes[0].action == BackfillAction.DEFAULT_VALUE

    def test_backfill_existing_value_not_overwritten(self):
        """Test backfill does not overwrite existing values."""
        backfiller = ToolArgBackfiller()
        schema = _create_test_schema()

        result = backfiller.backfill("test_tool", {"name": "test", "count": 20}, schema)
        assert result.success is True
        assert result.backfilled_args["count"] == 20

    def test_backfill_disabled_default_backfill(self):
        """Test backfill with disabled default backfill."""
        backfiller = ToolArgBackfiller(enable_default_backfill=False)
        schema = _create_test_schema()

        result = backfiller.backfill("test_tool", {"name": "test"}, schema)
        assert result.success is True
        assert "count" not in result.backfilled_args

    def test_backfill_changes_to_dict(self):
        """Test converting changes to dict format."""
        backfiller = ToolArgBackfiller()
        schema = _create_test_schema()

        result = backfiller.backfill("test_tool", {"name": "test"}, schema)
        changes_dict = [c.to_dict() for c in result.changes]
        assert len(changes_dict) == 1
        assert "param" in changes_dict[0]
        assert "action" in changes_dict[0]
        assert "original" in changes_dict[0]
        assert "new" in changes_dict[0]
