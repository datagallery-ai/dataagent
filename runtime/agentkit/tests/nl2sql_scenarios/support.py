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
"""Shared helpers for the scenario-pack tests (this package only).

The repository-level ``tests/conftest.py`` is shared by every other test suite,
so scenario-specific helpers stay here: the scenario tests import them from
``nl2sql_scenarios.support`` and that file needs no changes. ``ScriptedModel``
comes from the shared conftest and is re-exported for the same one-line import.
"""

import httpx
from conftest import ScriptedModel
from langchain_core.messages import AIMessage
from nl2sql.config_manager import NL2SQLConfig
from nl2sql.hooks import HookContext

from dataagent.semantic_client import SemanticServiceClient


def scripted_model(*contents):
    """A ScriptedModel replaying the given message contents, in order."""
    return ScriptedModel(responses=[AIMessage(content=content) for content in contents])


def make_context(scenario, model, semantic_client, *, db_id="business_twin") -> HookContext:
    """Build the HookContext the host factory passes to nl2sql ``load_scenario``."""
    config = NL2SQLConfig(
        {
            "model": "primary",
            "scenario": scenario,
            "database": {"db_id": db_id, "dialect": "postgres", "engine": "postgres"},
        }
    )
    return HookContext(
        scenario=scenario,
        model=model,
        config=config,
        semantic_client=semantic_client,
    )


def semantic_client(handler):
    """A semantic client wired to an httpx.MockTransport handler (no network)."""
    return SemanticServiceClient(
        "http://semantic.internal", transport=httpx.MockTransport(handler)
    )
