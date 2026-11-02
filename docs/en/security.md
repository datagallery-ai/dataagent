# Security

The control plane listens on `127.0.0.1` by default. Callers and the backend are on the same machine. HTTP does not check an access token.

## Secrets

Public examples use placeholders such as `replace-with-your-key`. Do not commit real model keys, semantic-service passwords, or MCP tokens, and do not put them in docs or screenshots.

Put secrets in the process environment or Home `.env`, and reference them with `$env{...}`. Logs and error text replace secrets the runtime knows. Trace files still contain the system prompt, the conversation, and tool results.

`POST /dataagent/stream` rejects `context`, `state`, and `forwardedProps`. Do not put secrets in the user message.

## Identity and files

`--user` only selects `runtime/users/<id>/`. It is not a login. Another user cannot read that session through the API. A local process that can read Home can still open the files directly.

New Home directories are mode `0700`. `config.json` and `.env` created at init are mode `0600`. Existing files keep their mode.

Workspaces are marked read-only in configuration. The system prompt tells the model to write new files under the session `outputs/`. The prompt does not change operating-system permissions. File tools and the shell use the control-plane process permissions. The default working directory is that session's `outputs/`.

Enabling a plugin loads and runs its Python in this process. MCP stdio commands also run in this process.
