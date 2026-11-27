# Troubleshooting

Save the error code and correlation IDs. Under `<home>/runtime/logs/`, use `backend.log` for the backend and plugins, `web.log` for the workbench and its proxy, and `tui.log` for terminal connection and interaction. Startup failures appear in backend JSON logs with `component=dataagent.startup` and stage `prepare` or `start`. Each file rotates at 5,000,000 bytes with three backups. See [observability](../../development/architecture/observability.md) for severity, stacks, and redaction.

## A2A failures

Save the `X-Request-ID` header and task ID. Read `metadata.dataagent.error.code`, `stage`, and `message`. A failed Task can arrive with HTTP 200. Polling and cancellation do not change the original `metadata.dataagent.requestId`.

Look for the ID in the current or rotated `backend.log`. `a2a.assembly.failed` reports the stage, error class, and stack frames. `a2a.trace.saved` links the native run ID and trace file. Input validation failures happen before a graph trace exists. Check request or rejection logs.

`checkpoint_settle` means saving the checkpoint after graph execution. Check storage even when the graph trace reports success. `a2a.checkpoint.failed` and `a2a.cleanup.failed` supplement the original error. Default anonymous team access does not identify the real caller. In-memory tasks disappear after restart, but sessions and checkpoints can resume. Old correlations remain searchable during log retention. The server does not automatically retry or deduplicate execution. An error code alone cannot establish that retrying is safe.

## Startup

| Symptom | Resolution |
| --- | --- |
| `Cannot determine user home; set DATAAGENT_HOME` | Set a writable `DATAAGENT_HOME` in the process environment |
| `Configuration file does not exist`, `Expected a configuration file`, or `invalid YAML syntax` | Check `--config`. Relative paths start at the command's working directory. The process exits with code 1 before binding |
| `unknown field` or duplicate keys | Remove unknown fields or duplicate YAML keys, including nested ones. Supported `schema_version` values are product 4, manifest 3, and subagent 1 |
| `missing environment variable LLM_...` | Set the variable in the process or selected `--env-file`. The default is Home `.env` |
| `Port 127.0.0.1:8790 is occupied` | Change `server.port` or stop the other listener. Startup does not take over that service |
| Engine unavailable | For `serve`, `/healthz` returns 200 with `data.conversation.ready: false`, and `/ready` and conversation calls return 503. Check startup errors, commonly missing model variables. Standalone `serve-a2a` has neither health route |
| `Another DataAgent backend owns this workspace` | A backend holds `backend.lock` for the same state directory. Stop it or use another Home |
| `Expected a directory` | Check for files occupying the Home `skills`, `hooks`, `plugins`, or `runtime` directories |

Set `DATAAGENT_HOME` and `DATAAGENT_INSTANCE_ID` only in the process environment. If logs say “Ignoring process-only variable,” remove it from `.env`.

## Models

| Symptom | Resolution |
| --- | --- |
| `dataagent.model does not name a configured model` | Use an ID under `models`, not the provider model name |
| `Unknown model alias` | Add the configured alias or change the tool's call |
| Timeout or 401 | Check `base_url` and `api_key`. Use the API root rather than the `/chat/completions` endpoint |
| Long retries ending in failure | `max_retries` defaults to 2 for up to three HTTP attempts. Set 0 to disable retries. An already-started agent loop is not replayed |

The default model resolves its environment variables at startup. Other models resolve them on first use. Restart after editing `.env`. Extension refresh does not reread model settings.

## Connections

| Symptom | Resolution |
| --- | --- |
| TUI cannot connect | Check `/healthz` on the Server address. The default is `http://127.0.0.1:8790`, overridden by Home `server.host` and `server.port`. Wildcard hosts use loopback. Use `--runtime-url` to attach only or `./deploy.sh tui` for a managed stack |
| Port busy with `--config` or other local startup flags | Those flags launch a backend. To attach to the existing control plane at its configured address, run `npm run start:tui` without them |
| MCP discovery fails | Fix the Home or plugin `.mcp.yaml` and restart. The process remains up, but conversation paths return 503 |
| Semantic TLS fails | `verify_server: false` disables verification. If verification needs a custom CA, provide a readable `ca_file`. A client certificate and key must be supplied together |

## Resume

| Symptom | Resolution |
| --- | --- |
| `Session ... does not belong to user` | Use the original `--user` or start a new session |
| `bound to a different workspace list` | Resume with the bound inputs or open a new session |
| First turn failed and the session cannot continue | Send a new message with a fresh `runId`. Without a checkpoint, `/api/v1/runs` uses the latest graph state. Cancellation aligns the next turn and the UI with the checkpoint |
| HTTP 409, `A run is already active for this session` | Wait or cancel the active turn. The session can still be read while it runs |
| `Run replay is not supported` | Send a new user message with a fresh run ID |
| `Extension reload failed` | Correct the plugin or Hook and try again. Existing turns continue on their prior graph |
| `Output directory is used by session ...` | Choose a nonoverlapping directory for the new session. See [output errors](sessions-and-outputs.md#common-errors) |
| `Output directory ... already contains files that no session owns` | Choose an empty directory or use a reviewed directory with explicit SDK `output_dir` |

## Empty outputs

`/outputs` lists regular files in the bound output directory. The directory can be outside Home. Its location is recorded in `session_bindings` in Home's `sessions.sqlite`. The listing skips `artifacts/conversation_history`, `artifacts/large_tool_results`, symbolic links, and files written elsewhere. Permission errors surface as errors.
