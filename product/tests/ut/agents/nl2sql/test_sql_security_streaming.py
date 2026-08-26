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
from dataagent.agents.nl2sql.security.streaming import sanitize_stream_item
from dataagent.agents.nl2sql.workflow.state import Result


def test_sanitize_stream_item_hides_unapproved_candidates() -> None:
    """Streaming should not expose SQL candidates before Reflector approval."""
    item = (
        "values",
        {
            "sql": "SELECT current_setting('x')",
            "generation_results": [Result(id=0, sql="SELECT current_setting('x')")],
            "stream_message": "=== Generator ===\nSELECT current_setting('x')",
            "security_sql_approved": False,
        },
    )

    sanitized = sanitize_stream_item(item)

    assert sanitized[1].get("sql", "missing") == ""
    assert sanitized[1].get("generation_results", ["missing"]) == []
    assert "current_setting" not in sanitized[1].get("stream_message", "")


def test_sanitize_stream_item_hides_nested_candidate_updates() -> None:
    """Updates-mode node payloads should be sanitized recursively."""
    item = (
        "updates",
        {
            "generator": {
                "generation_results": [Result(id=0, sql="SELECT current_setting('x')")],
                "stream_message": "=== Generator ===\nSELECT current_setting('x')",
            }
        },
    )

    sanitized = sanitize_stream_item(item)

    generator = sanitized[1].get("generator", {})
    assert generator.get("generation_results", ["missing"]) == []
    assert "current_setting" not in generator.get("stream_message", "")


def test_sanitize_stream_item_keeps_approved_safe_result() -> None:
    """Streaming should preserve a candidate explicitly approved by Reflector."""
    item = (
        "updates",
        {
            "reflector": {
                "sql": "SELECT id FROM orders WHERE id = 1",
                "validation_results": [Result(id=0, sql="SELECT id FROM orders WHERE id = 1")],
                "security_sql_approved": True,
            }
        },
    )

    assert sanitize_stream_item(item) == item
