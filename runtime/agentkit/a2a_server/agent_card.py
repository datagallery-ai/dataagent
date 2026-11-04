"""Build an A2A 1.0 Agent Card for an AgentKit runtime."""

from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)

from dataagent import __version__

JSONRPC_PATH = "/a2a/jsonrpc"


def _card_host(host: str) -> str:
    """Publish a dialable host. A wildcard bind address is not a client URL."""
    bare = host.strip()
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1].strip()
    if bare in {"0.0.0.0", "::", "::0", ""}:
        return "127.0.0.1"
    if ":" in bare:
        return f"[{bare}]"
    return bare


def build_agent_card(
    *,
    host: str,
    port: int,
    name: str = "DataAgent",
    description: str = "AgentKit data agent",
) -> AgentCard:
    """Describe this process as one A2A agent with a single JSON-RPC binding.

    The executor waits for the whole graph, so the card does not advertise streaming.
    """
    base_url = f"http://{_card_host(host)}:{port}"
    return AgentCard(
        name=name,
        description=description,
        version=__version__,
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="chat",
                name="Chat",
                description="Send a text message and receive the agent's final answer",
                tags=["chat", "data-agent"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            ),
        ],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=f"{base_url}{JSONRPC_PATH}",
                protocol_version="1.0",
            ),
        ],
    )
