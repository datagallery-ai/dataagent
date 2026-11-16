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
"""Shared LLM plumbing for the UDN scenario packs.

Renders a pack's ``<action>system.md`` / ``<action>user.md`` prompt pair the
way the base nodes render theirs (system read as-is, user rendered with
jinja2) and invokes the assembly-bound model — the factory's budgeted
delegate, so every call counts against the task budget.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from nl2sql.errors import DataAgentError
from nl2sql.prompt_manager import PromptTemplate
from nl2sql.utils.nl2sql_utils import json_parser

from dataagent import LoopDetectedError


async def execute_with_llm(
    model: BaseChatModel,
    prompt_dir: Path,
    action: str,
    context: dict[str, str],
) -> str:
    """Render the pack's ``<action>`` prompt pair and invoke the bound model."""
    system_prompt = PromptTemplate.from_file(str(prompt_dir / f"{action}system.md")).content
    user_template = PromptTemplate.from_file(str(prompt_dir / f"{action}user.md"))
    user_prompt = user_template.apply_prompt_template(**context)
    response = await model.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
    return response.content


async def execute_with_llm_json(
    model: BaseChatModel,
    prompt_dir: Path,
    action: str,
    context: dict[str, str],
) -> Any:
    """Execute the model and return the parsed JSON output (three attempts)."""
    for attempt in range(3):
        try:
            return json.loads(json_parser(await execute_with_llm(model, prompt_dir, action, context)))
        except LoopDetectedError:
            # Scenario model calls have the same terminal-error contract as base nodes.
            raise
        except (DataAgentError, json.JSONDecodeError) as exc:
            if attempt == 2:
                if isinstance(exc, DataAgentError):
                    raise
                raise DataAgentError(
                    source="internal",
                    fact=f"Model output format error: JSON parsing failed after 3 attempts: {exc}",
                    component="nl2sql",
                ) from exc
    return None
