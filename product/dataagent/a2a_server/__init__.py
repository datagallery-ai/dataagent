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

"""DataAgent A2A 1.0 Server — exposes DataAgent agents as A2A-compliant services."""

from dataagent.a2a_server.agent_card import build_agent_card
from dataagent.a2a_server.agent_executor import DataAgentExecutor
from dataagent.a2a_server.server import create_a2a_server, run_a2a_server

__all__ = [
    "DataAgentExecutor",
    "build_agent_card",
    "create_a2a_server",
    "run_a2a_server",
]
