"""Read the two MCP configuration sources and translate them to native connection dicts.

No MCP client imports or connections during runtime preparation.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from dataagent.bootstrap.config_files import expand_env, freeze, validate_against
from dataagent.config.declarations import MCPConfig, StdioMCPServer
from dataagent.config.strict_yaml import read_yaml


def load_mcp_config(
    home: Path, plugin_roots: Sequence[Path], env: Mapping, *, dotenv: Path | None = None
) -> Mapping:
    connections = {}
    sources = [("user", home), *((root.name, root) for root in plugin_roots)]
    for scope, root in sources:
        path = root / ".mcp.yaml"
        if not path.exists() and not path.is_symlink():
            continue
        if not path.is_file():
            raise ValueError(f"Expected an MCP configuration file: {path}")
        data = expand_env(read_yaml(path), env, source=path, home=home, dotenv=dotenv)
        config = validate_against(MCPConfig, data, path)
        for name, server in config.mcpServers.items():
            identity = f"mcp__{scope}__{name}"
            if identity in connections:
                raise ValueError(f"Duplicate MCP server: {identity}")
            if isinstance(server, StdioMCPServer):
                command = server.command
                if "/" in command and not Path(command).is_absolute():
                    command = str((root / command).resolve())
                connections[identity] = {
                    "transport": "stdio",
                    "command": command,
                    "args": server.args,
                    "env": server.env,
                    "cwd": str(root),
                }
            else:
                connections[identity] = {
                    "transport": "streamable_http" if server.type == "http" else "sse",
                    "url": str(server.url),
                    "headers": server.headers,
                }
    return freeze(connections)
