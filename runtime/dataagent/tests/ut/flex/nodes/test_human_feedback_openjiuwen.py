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

from types import SimpleNamespace

from dataagent.core.flex.nodes.human_feedback import HumanFeedbackNode


def test_clear_human_feedback_resume_calls_update_global_state() -> None:
    calls: list[dict[str, str]] = []
    runtime = SimpleNamespace(update_global_state=lambda payload: calls.append(payload))

    HumanFeedbackNode._clear_human_feedback_resume_on_runtime(runtime)

    assert calls == [{"__human_feedback_resume__": ""}]


def test_clear_human_feedback_resume_falls_back_to_update_global_state() -> None:
    calls: list[dict[str, str]] = []
    runtime = SimpleNamespace(update_global_state=lambda payload: calls.append(payload), state=lambda: None)

    HumanFeedbackNode._clear_human_feedback_resume_on_runtime(runtime)

    assert calls == [{"__human_feedback_resume__": ""}]
