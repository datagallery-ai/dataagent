# Capabilities

This page compares the two presentations with the control plane: what you can do when trying the product. It does not list plugin-author declarations or runtime internals.

The Web UI may still show older resource panels. Those requests return 404 and are not current capabilities.

| Capability | Web | TUI | Control plane |
| --- | --- | --- | --- |
| Conversation | Ask from `/data-tasks` | Type in the terminal | `POST /api/v1/runs` |
| Session list | Shares the index with TUI | `/resume` | `GET /api/v1/sessions` |
| Outputs | Through the session routes | `/outputs` | `GET /api/v1/sessions/{threadId}/outputs` |
| Cancel one turn | Call the cancel route | Disconnecting interrupts the turn | `POST /api/v1/runs/{runId}/cancel` |
| Model | Uses the configuration already loaded | Same | `models` in `config.json`, plus `.env` |
| Read-only input | Uses directories bound at startup | Same | `dataagent.workspaces` or `--workspace` |
| Skills / plugins / MCP | Uses declarations enabled under Home | Same | `<home>/skills`, `plugins`, `.mcp.json` |
| Health | Proxied on the same origin | May read `/healthz` before connecting | `GET /healthz`, `GET /ready` |
| Identity | `GET /api/v1/me` returns `--user` | No login | No registration, cookie, or CSRF |

These routes are absent: `/api/v1/capabilities`, datasource registration, knowledge bases, a workspace-config REST API, creating or testing model profiles, session rename, delete, branches, and a trace DAG.

Database questions exist only after the matching plugin is enabled and its own `plugins.config` is set. That is not a built-in datasource service on the control plane.
