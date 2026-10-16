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
"""NL2SQL sub-agent plugin package.

Self-contained NL2SQL workflow with all ``dataagent`` core dependencies
replaced by plugin-local modules:

- parameters: :mod:`nl2sql.config_manager` (the Agent configuration's
  ``plugins.config.nl2sql`` JSON section)
- workflow: langgraph-native :mod:`nl2sql.graph` over
  :class:`~nl2sql.nodes.BaseNL2SQLNode` objects, with the chat model and
  semantic client injected by the compiled-subagent factory
  (``subagents/nl2sql.py``)
"""

from nl2sql.config_manager import NL2SQLConfig
from nl2sql.errors import DataAgentError
from nl2sql.graph import build_nl2sql_graph
from nl2sql.state import NL2SQLState, Result

__all__ = [
    "NL2SQLConfig",
    "NL2SQLState",
    "Result",
    "DataAgentError",
    "build_nl2sql_graph",
]
