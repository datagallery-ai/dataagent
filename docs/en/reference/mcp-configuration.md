# MCP configuration

DataAgent reads host MCP connections from `.mcp.yaml` in Home and in enabled plugin roots. Files use strict YAML 1.2. See [MCP integration](../guides/integrate/extensions/mcp.md) for usage and [mcp.full.yaml](../../../examples/mcp/mcp.full.yaml) for all transports.

## Top level { #top-level }

The only top-level field is required `mcpServers`. It maps names matching `[a-z][a-z0-9_-]{0,63}` to connections below. Use `{}` for an empty map. Remove an entry or disable its plugin to remove a connection. Product settings and plugin manifests have separate schemas.

## stdio { #stdio }

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | `"stdio"` | `"stdio"` | Can be omitted |
| `command` | Nonempty string | Required | Executable or program name |
| `args` | List of strings | `[]` | One literal argument per entry |
| `env` | String map | `{}` | Additional child environment |

The child runs in the directory containing `.mcp.yaml`. Relative commands containing `/` resolve from that directory. Bare names use the launch environment. The executable and arguments are passed directly without shell splitting.

## HTTP and SSE { #remote-transports }

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | `"http"` or `"sse"` | Required | Streamable HTTP or SSE |
| `url` | HTTP(S) URL | Required | Complete MCP endpoint |
| `headers` | String map | `{}` | Headers required by the service, including authentication |

Unknown fields and fields from another transport are rejected.

## Environment and paths { #environment-and-paths }

`$env{NAME}` expands after YAML parsing and before field validation. Use it in executable paths, environment values, and headers. Missing variables fail runtime preparation. Nonempty process values override the selected environment file, defaulting to Home `.env` unless `--env-file` is used. Plugin-local `.env` files are not loaded automatically. See [environment precedence](cli-and-environment.md#environment-variables).

Merge the template entries you need into the actual `.mcp.yaml` and supply reachable endpoints and credentials before startup.

## Tool naming and discovery failures { #tool-naming }

The service ID is `mcp__<scope>__<server>`. Home scope is `user`; plugins use the directory name. Duplicate service IDs fail runtime preparation. The registered tool name adds `__<original-tool-name>`, for example `mcp__user__math__add`. The upstream call still uses the original name.

Full names must match `[A-Za-z0-9_-]{1,64}`. Duplicate MCP tool names fail discovery, and collisions with other registered tools fail compilation. Discovery is limited to the smaller of 20 seconds and `dataagent.limits.timeout_seconds`. Tool calls also use the configured timeout for transport waiting. See [discovery and refresh](../guides/integrate/extensions/mcp.md#discovery-and-refresh).
