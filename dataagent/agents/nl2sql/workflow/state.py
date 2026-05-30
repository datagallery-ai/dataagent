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
from typing import Any, Literal

from dataagent.core.cbb.base_state import BaseState


@dataclass
class Result:
    id: int
    sql: str
    prompt: str = ""
    strategy: Literal["prompt", "skeleton", "icl", "dc"] = "prompt"
    score: float = 0.0  # reflector
    confidence: float = 0.0  # selector
    issues: list[str] = field(default_factory=list)
    columns: list[str] | None = field(default_factory=list)
    rows: list[tuple[Any, ...]] | None = field(default_factory=list)
    rows_preview: list[tuple[str, ...]] | None = field(default_factory=list)
    error: str | None = None
    need_ref: bool = False


class NL2SQLState(BaseState):
    question: str

    # output
    sql: str
    confidence: float
    columns: list[str] | None
    rows: list[tuple[Any, ...]] | None
    rows_preview: list[tuple[str, ...]] | None

    # coordinator
    semantic_question: str
    keywords: list[str]

    # perceptor
    schema: dict
    joins: list[tuple[str, str]]
    schema_str: str  # backdoor for schema injection
    few_shot_examples: str
    sql_rules: str
    evidence: str

    # generator

    generation_results: list[Result]

    # validator
    validation_results: list[Result]

    # reflector
    ref_retries: int
    proceed: bool

    # executor
    execution_results: list[Result]

    # selector
    sel_retries: int

    # streaming
    stream_message: str


def get_default_state(question: str, **override) -> NL2SQLState:
    default_state = {
        "messages": [],
        "question": question,
        "sql": "",
        "confidence": 0.0,
        "columns": None,
        "rows": None,
        "rows_preview": None,
        "semantic_question": "",
        "keywords": [],
        "schema": {},
        "joins": [],
        "schema_str": "",
        "few_shot_examples": "",
        "evidence": "",
        "sql_rules": "",
        "generation_results": [],
        "validation_results": [],
        "ref_retries": 2,
        "proceed": True,
        "execution_results": [],
        "sel_retries": 1,
        "stream_message": "",
    }
    default_state.update(override)
    return default_state
