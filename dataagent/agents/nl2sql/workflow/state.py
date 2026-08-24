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
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from dataagent.core.cbb.base_state import BaseState
from dataagent.utils.constants import DEFAULT_NL2SQL_REF_RETRIES, DEFAULT_NL2SQL_SEL_RETRIES


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
    security_checked: bool = False
    security_violations: list[dict[str, str]] = field(default_factory=list)


class NL2SQLState(BaseState):
    question: str

    # output
    sql: str
    confidence: float
    columns: list[str] | None
    rows: list[tuple[Any, ...]] | None
    rows_preview: list[tuple[str, ...]] | None

    # perceptor
    keywords: list[str]
    schema: dict
    joins: list[tuple[str, str]]
    schema_str: str
    few_shot_examples: str
    sql_rules: str
    evidence: str

    # generator
    generation_results: list[Result]

    # validator
    validation_results: list[Result]
    security_sql_approved: bool

    # reflector
    ref_retries: int
    proceed: bool

    # executor
    execution_results: list[Result]

    # selector
    sel_retries: int

    # streaming
    stream_message: str


def _reset_result_security(results: Any) -> list[Any]:
    """Return copies of ``results`` with the security verdict cleared."""
    cleaned: list[Any] = []
    for item in results or []:
        if isinstance(item, Result):
            reset_result = replace(item, security_checked=False, security_violations=[])
            cleaned.append(reset_result)
        elif isinstance(item, dict):
            reset = dict(item)
            reset["security_checked"] = False
            reset["security_violations"] = []
            cleaned.append(reset)
        else:
            cleaned.append(item)
    return cleaned


def get_default_state(question: str, **override) -> NL2SQLState:
    """Return a fresh NL2SQLState with default field values."""
    default_state = {
        "messages": [],
        "question": question,
        "sql": "",
        "confidence": 0.0,
        "columns": None,
        "rows": None,
        "rows_preview": None,
        "keywords": [],
        "schema": {},
        "joins": [],
        "schema_str": "",
        "few_shot_examples": "",
        "evidence": "",
        "sql_rules": "",
        "generation_results": [],
        "validation_results": [],
        "security_sql_approved": False,
        "ref_retries": DEFAULT_NL2SQL_REF_RETRIES,
        "proceed": True,
        "execution_results": [],
        "sel_retries": DEFAULT_NL2SQL_SEL_RETRIES,
        "stream_message": "",
    }
    default_state.update(override)
    default_state["security_sql_approved"] = False
    default_state["validation_results"] = _reset_result_security(default_state.get("validation_results"))
    default_state["generation_results"] = _reset_result_security(default_state.get("generation_results"))
    default_state["execution_results"] = _reset_result_security(default_state.get("execution_results"))
    return default_state
