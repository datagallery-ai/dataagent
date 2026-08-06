# DataLink semantic service

DataLink is an optional semantic graph service for DataFoundry. It connects schemas and profiles to business concepts, entities, join paths, and confidence-scored relationships. The implementation is included under `services/datalink`, but it runs as a separate Python service.

## Runtime topology

| Process | Default endpoint | Purpose |
| --- | --- | --- |
| DataLink MCP | `http://127.0.0.1:8080/mcp` | Exposes `datalink_explore` for agent grounding |
| DataLink REST | `http://127.0.0.1:8081` | Graph management and visualization API |

DataFoundry's Web and API processes remain unchanged. DataLink is not started by `deploy.sh` or `npm run start`; start it separately when semantic grounding is needed.

## Install and start

From the service directory, install Python 3.10+ and [uv](https://docs.astral.sh/uv/):

```bash
cd services/datalink
uv pip install -e .
cp datalink_config.example.json datalink_config.json
```

Set the LLM and embedding provider in `datalink_config.json`, then start both processes in separate terminals:

```bash
uv run datalink serve --port 8080 --transport streamable-http
uv run datalink api --port 8081
```

The service README contains provider examples and CLI commands for building and exploring a graph.

## Connect it in DataFoundry

In the Web workbench, open MCP settings and add an external server with:

| Field | Example |
| --- | --- |
| `serverUrl` | `http://127.0.0.1:8080/mcp` |
| `apiUrl` | `http://127.0.0.1:8081` |
| `transport` | `streamable-http` |
| `toolManifest` | `[{ "name": "datalink_explore" }]` |

Use a name or id containing `datalink` so the DataLink panel recognizes the server.

## Verify

```bash
curl http://127.0.0.1:8081/healthz
```

If the panel reports the service as unavailable, check both processes, ports `8080` and `8081`, the configured graph path, and the MCP transport. Keep API keys in environment variables or a secret manager; do not commit them to `datalink_config.json`.
