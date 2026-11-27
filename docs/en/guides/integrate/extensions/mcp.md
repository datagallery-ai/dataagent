# MCP integration

Use `.mcp.yaml` to make external MCP tools available to DataAgent. Put shared connections in Home and plugin-specific connections at the plugin root. See [MCP configuration](../../../reference/mcp-configuration.md) for locations, transports, and fields.

<a name="local-stdio-example"></a>

## Run the local stdio example

From the repository root, prepare Python and a separate Home:

```bash
uv sync --locked
export MCP_PYTHON="$(uv run --no-sync python -c 'import sys; print(sys.executable)')"
export DATAAGENT_HOME="$(mktemp -d)"
cp examples/mcp/.mcp.yaml examples/mcp/math_server.py "$DATAAGENT_HOME/"
```

The [example configuration](../../../../../examples/mcp/.mcp.yaml) uses `MCP_PYTHON` to start its neighboring [math_server.py](../../../../../examples/mcp/math_server.py). In an existing Home, merge just the `math` entry into `mcpServers` and put the script next to the configuration. Check for an existing entry or script before copying.

Configure a model for that Home with the [configuration quick reference](../../configuration.md), then run the [built TUI](../../tui.md) in the same terminal against its Server. Ask `Use mcp__user__math__add to add 12 and 30.` The result should be 42. If another Server owns the default port, configure a different listen address for this Home and attach to it. See [CLI reference](../../../reference/cli-and-environment.md#dataagent-serve).

To test tool discovery and invocation with a temporary Home and no live model:

```bash
uv run --no-sync pytest -q tests/test_mcp.py::test_documented_local_example_is_runnable
```

<a name="remote-mcp"></a>

## Connect a remote MCP server

Merge the needed `mcpServers` entries from the [complete template](../../../../../examples/mcp/mcp.full.yaml). Supply the full HTTP(S) MCP URL and choose Streamable HTTP (`type: http`) or SSE (`type: sse`) as required. Declare authentication in `headers` and reference credentials as `$env{NAME}`. See [environment and paths](../../../reference/mcp-configuration.md#environment-and-paths).

<a name="discovery-and-refresh"></a>

## Discovery, refresh, and invocation

Discovery runs before the conversation engine becomes ready. If it fails, `dataagent serve` keeps `/healthz` at 200 with `data.conversation.ready: false`. `/ready` and conversation routes return 503. Look for `MCP discovery failed` or `MCP discovery timed out` in `backend.log`, fix configuration or connectivity, then restart.

A ready server checks local extension changes before a new turn. MCP rediscovery occurs only when the effective connection map changes. A failed refresh rejects that turn while existing turns retain their tools. Restart to rediscover remote tools if local configuration has not changed. See the [extension lifecycle](../../../../development/architecture/extension-lifecycle.md).

Each tool call opens and closes its own MCP session. A stdio server starts for each call. Use files or an external service for state that must persist across calls.

<a name="python-sdk"></a>

## Use MCP in the Python SDK

Discover tools asynchronously and pass them as `mcp_tools` to `create_agent`. The [SDK guide](../sdk.md) shows how to obtain `runtime` with `prepare_runtime`.

```python
from dataagent import create_agent, load_mcp_tools

agent = create_agent(runtime, mcp_tools=await load_mcp_tools(runtime))
```

SDK hosts manage tool loading and updates explicitly. If MCP is configured and tools are omitted, agent creation reports the missing tools.

<a name="nl2sql-mcp"></a>

## NL2SQL over MCP

Use `plugins.config.nl2sql.database.engine: mcp` and `database.path` for an independent Streamable HTTP database connection. NL2SQL invokes its configured reader for queries and EXPLAIN. See the [NL2SQL plugin](../../../../../dataagent/plugins/nl2sql/README.md) for endpoints, reader settings, and responses.

Host `.mcp.yaml` authentication, discovery, hooks, and tool counts apply to host tools. NL2SQL manages its database connection and SQL execution through its own configuration.
