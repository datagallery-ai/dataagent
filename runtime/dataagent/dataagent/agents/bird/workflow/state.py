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
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired

from dataagent.agents.bird.constants import DEFAULT_BIRD_REF_RETRIES
from dataagent.core.cbb.base_state import BaseState


@dataclass
class Result:
    id: int
    sql: str
    prompt: str = ""
    strategy: Literal["dc", "skeleton", "icl"] = "dc"
    phase: Literal[1, 2] = 1
    score: float = 0.0  # reflector
    confidence: float = 0.0  # selector
    issues: list[str] = field(default_factory=list)
    columns: list[str] | None = field(default_factory=list)
    rows: list[tuple[Any, ...]] | None = field(default_factory=list)
    rows_preview: list[tuple[str, ...]] | None = field(default_factory=list)
    error: str | None = None
    security_checked: bool = False
    security_violations: list[dict[str, str]] = field(default_factory=list)
    validation_passed: bool = False
    syntax_validated: bool = False


class BirdState(BaseState):
    question: str

    # output
    sql: str
    confidence: float
    columns: list[str] | None
    rows: list[tuple[Any, ...]] | None
    rows_preview: list[tuple[str, ...]] | None
    error: str | None

    # perceptor
    schema: dict
    schema_str: str
    few_shot_examples: str
    few_shot_lookup_complete: NotRequired[bool]
    sql_rules: str
    evidence: str
    schema_linking_trace: NotRequired[dict[str, Any] | None]
    schema_linking_error: NotRequired[str]

    # generator
    generation_results: list[Result]
    generation_phase: NotRequired[Literal[1, 2]]
    generation_warnings: NotRequired[list[dict[str, Any]]]
    executable_results: NotRequired[list[Result]]
    generation_targets: NotRequired[dict[str, int]]
    generation_attempts: NotRequired[dict[str, int]]
    generation_slots: NotRequired[dict[str, int]]
    generation_max_route_attempts: NotRequired[int]
    needs_generation_retry: NotRequired[bool]
    budget_complete: NotRequired[bool]
    needs_phase2: NotRequired[bool]

    # validator
    validation_results: list[Result]
    security_sql_approved: bool

    # reflector
    ref_retries: int
    proceed: bool

    # executor
    execution_results: list[Result]

    # streaming
    stream_message: str


def get_default_state(question: str, **override) -> BirdState:
    """Return a fresh BirdState with default field values."""
    default_state = {
        "messages": [],
        "question": question,
        "sql": "",
        "confidence": 0.0,
        "columns": None,
        "rows": None,
        "rows_preview": None,
        "error": None,
        "schema": {},
        "schema_str": "",
        "few_shot_examples": "",
        "evidence": "",
        "sql_rules": "",
        "generation_results": [],
        "validation_results": [],
        "security_sql_approved": False,
        "ref_retries": DEFAULT_BIRD_REF_RETRIES,
        "proceed": True,
        "execution_results": [],
        "stream_message": "",
    }
    default_state.update(override)
    return default_state
