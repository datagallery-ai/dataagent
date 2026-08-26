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
"""Build A2A 1.0 AgentCard from a DataAgent."""

from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)

from dataagent.interface.sdk.agent import DataAgent


def build_agent_card(
    agent: DataAgent,
    host: str = "0.0.0.0",
    port: int = 9999,
    jsonrpc_path: str = "/a2a/jsonrpc",
    rest_path: str = "/a2a/rest",
) -> AgentCard:
    """Create an A2A 1.0 AgentCard for the given DataAgent.

    Args:
        agent: The DataAgent instance to expose.
        host: Server host for the interface URLs.
        port: Server port for the interface URLs.
        jsonrpc_path: JSON-RPC route path.
        rest_path: REST route path.

    Returns:
        A2A 1.0 AgentCard with supported_interfaces.
    """
    agent_name = agent.name() or "DataAgent"
    agent_desc = agent.description() or "DataAgent data analysis agent"
    agent_ver = agent.version() or "0.1.0"

    # AgentCard URL should use 127.0.0.1 for local access (host may be 0.0.0.0 for binding)
    card_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    base_url = f"http://{card_host}:{port}"

    # Build skills from agent info and known capabilities
    skills = [
        AgentSkill(
            id="chat",
            name="Chat",
            description="Interactive conversational data analysis",
            tags=["data-analysis", "chat"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        ),
    ]

    card = AgentCard(
        name=agent_name,
        description=agent_desc,
        version=agent_ver,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        skills=skills,
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=f"{base_url}{jsonrpc_path}",
                protocol_version="1.0",
            ),
            AgentInterface(
                protocol_binding="HTTP+JSON",
                url=f"{base_url}{rest_path}",
                protocol_version="1.0",
            ),
        ],
    )
    return card
