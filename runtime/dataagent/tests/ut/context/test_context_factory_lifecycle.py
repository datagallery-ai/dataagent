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
"""Regression tests for bounded ContextFactory request lifecycle."""

from pathlib import Path

from dataagent.core.context.context import ContextFactory, ContextInitOptions


def test_release_context_removes_exact_run(tmp_path: Path) -> None:
    """A completed request can release its Context without clearing other runs."""
    ContextFactory.clear_context()
    first = ContextFactory.get_context(
        user_id="anonymous",
        session_id="request-a",
        run_id=0,
        sub_id=0,
        options=ContextInitOptions(workspace=tmp_path / "request-a"),
    )
    second = ContextFactory.get_context(
        user_id="anonymous",
        session_id="request-b",
        run_id=0,
        sub_id=0,
        options=ContextInitOptions(workspace=tmp_path / "request-b"),
    )

    released = ContextFactory.release_context(user_id="anonymous", session_id="request-a", run_id=0, sub_id=0)

    assert released == 1
    assert ContextFactory.get_context("anonymous", "request-b", 0, 0) is second
    assert ContextFactory.get_context("anonymous", "request-a", 0, 0) is not first
    ContextFactory.clear_context()
