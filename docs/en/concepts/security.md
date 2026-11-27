# Security

The control plane listens on `127.0.0.1` by default. REST does not check an access token. A2A JSON-RPC can require a shared Bearer token through `DATAAGENT_A2A_AUTH_TOKEN`; the Agent Card remains public. This token does not protect REST or identify individual callers. See [REST](../reference/rest-api.md) and [A2A](../reference/a2a.md).

## Secrets

Public examples use placeholders such as `replace-with-your-key`. Do not commit real model keys, semantic-service passwords, or MCP tokens, and do not put them in docs or screenshots.

Put secrets in the process environment or Home `.env`, and reference them with `$env{...}`. Logs and error text replace secrets the runtime knows. Traces can contain system prompts, conversation history, and tool results. Review this data before sharing a local trace or enabling remote export. [Trace handling](../../development/architecture/observability.md#trace)

Keep secrets out of user messages. See the [AG-UI request contract](../reference/events.md#request) for the accepted client fields.

## Identity and files

`--user` only selects `runtime/users/<id>/`. It is not a login. Another user cannot read that session through the API. A local process that can read Home can still open the files directly.

New Home directories are mode `0700`. `config.yaml` and `.env` created at init are mode `0600`. Existing files keep their mode.

The workspace's read-only setting guides the Agent through its system prompt. File tools and the shell use the control plane's operating-system permissions, and the shell starts in the session's output directory. Use operating-system permissions to enforce read-only access.

Enabling a plugin loads and runs its Python in the backend process. An MCP stdio service runs as a subprocess launched by the backend. [Developer security boundaries](../../development/architecture/security-boundaries.md)
