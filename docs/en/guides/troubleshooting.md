# Troubleshooting

Start with the symptom, then check the component and save the IDs for the failed request. In a split deployment, Server, Web, and TUI can run on different machines. Read each process's logs in its own DataAgent Home. Logs are under `<home>/runtime/logs/`. [Find the Home directory](../../development/architecture/home-layout.md)

| Symptom | First check | Log and identifiers |
| --- | --- | --- |
| A managed deployment failed, or the workbench does not open | Run `./deploy.sh status` and `./deploy.sh doctor` in the repository. Inspect your launch terminal for a standalone service. | `backend.log` for environment preparation or service startup. `web.log` for Web build, page, or proxy failures. Record the failure time and deployment stage. |
| Workbench opens, but a turn fails | Check Server liveness and readiness, then inspect the response or the AG-UI `RUN_ERROR`. | `backend.log`. Save the error code, `X-Request-ID` if available, `threadId`, and `runId`. |
| TUI cannot connect or the screen behaves unexpectedly | Check the TUI's target Server address and its `/ready` response. | `tui.log` in the TUI process's Home. If the Server responds with an error, match the time and run ID in the Server's `backend.log`. |
| A2A request or Task fails | Read the HTTP status, JSON-RPC response, and Task status. | `backend.log`. Save the `X-Request-ID`, Task ID, and the error code and stage. |
| A run completed but its trace is missing | Check the Server or SDK process's Home and look for `TRACE_SAVE_ERROR` in the host logs. | Use the `threadId` to find the trace directory and correlate the native run ID. See [execution traces](../../development/architecture/execution-traces.md). |

For a shell on a managed Linux deployment, follow all three logs with `./deploy.sh logs`. For a standalone process, check the launch terminal if its log file is missing. The backend can fall back to stderr. Check rotated files when the error precedes the current log. Keep credentials, prompts, and business data out of shared excerpts. [Log format and retention](../../development/architecture/observability.md)

Search the backend log and its rotated files with an actual request or run ID:

```bash
home="/path/to/dataagent-home"
id="your-request-or-run-id"
grep -F -- "$id" "$home"/runtime/logs/backend.log*
```

REST diagnostics use `requestId`, `threadId`, and `runId`. A2A audit records use `request_id` and `task_id`. Include the failure time when the client has no ID.

For direct calls to `dataagent serve`, substitute the address used by the Server:

```bash
curl -i http://127.0.0.1:8790/healthz
curl -i http://127.0.0.1:8790/ready
```

`/healthz` returning 200 confirms the process is responding. If `/ready` returns 503, check startup or extension discovery logs. A ready engine can still report model or tool failures for a specific turn. Standalone `serve-a2a` uses its [Agent Card and JSON-RPC endpoints](../reference/a2a.md).

<a name="a2a-failures"></a>

## A2A failures

Save the `X-Request-ID` header and task ID. Read `metadata.dataagent.error.code`, `stage`, and `message`. A failed Task can arrive with HTTP 200. Polling and cancellation do not change the original `metadata.dataagent.requestId`.

Look for `request_id` or `task_id` in the current or rotated `backend.log`. `a2a.assembly.failed` reports the stage, error class, and stack frames. `a2a.trace.saved` links the native run ID and trace file. Input validation failures happen before a graph trace exists. Check request or rejection logs.

`checkpoint_settle` means saving the checkpoint after graph execution. Check storage even when the graph trace reports success. `a2a.checkpoint.failed` and `a2a.cleanup.failed` supplement the original error. Default anonymous team access does not identify the real caller. In-memory tasks disappear after restart, but sessions and checkpoints can resume. Old correlations remain searchable during log retention. The server does not automatically retry or deduplicate execution. An error code alone cannot establish that retrying is safe.

<a name="startup"></a>

## Startup

For backend startup failures, look for JSON records with `component=dataagent.startup` and `stage=prepare` or `stage=start`. Startup may fail before an Agent run or trace ID exists.

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

<a name="models"></a>

## Models

| Symptom | Resolution |
| --- | --- |
| `dataagent.model does not name a configured model` | Use an ID under `models`, not the provider model name |
| `Unknown model alias` | Add the configured alias or change the tool's call |
| Timeout or 401 | Check `base_url` and `api_key`. Use the API root rather than the `/chat/completions` endpoint |
| Long retries ending in failure | `max_retries` defaults to 2 for up to three HTTP attempts. Set 0 to disable retries. An already-started agent loop is not replayed |

The default model resolves its environment variables at startup. Other models resolve them on first use. Restart after editing `.env`. Extension refresh does not reread model settings.

<a name="connections"></a>

## Connections

| Symptom | Resolution |
| --- | --- |
| TUI cannot connect | Check `/healthz` on the Server address. The default is `http://127.0.0.1:8790`, overridden by Home `server.host` and `server.port`. Wildcard hosts use loopback. Use `--runtime-url` to attach only or `./deploy.sh tui` for a managed stack |
| Port busy with `--config` or other local startup flags | Those flags start a backend. To attach to the intended Server, pass its address explicitly with `--runtime-url`, or use `./deploy.sh tui` for your managed deployment |
| MCP discovery fails | Check `backend.log` for the failed MCP server. During initial discovery, `serve` can keep its health endpoint up while conversation routes return 503. Fix the Home or plugin `.mcp.yaml` and restart |
| Semantic TLS fails | `verify_server: false` disables verification. If verification needs a custom CA, provide a readable `ca_file`. A client certificate and key must be supplied together |

<a name="resume"></a>

## Resume

| Symptom | Resolution |
| --- | --- |
| `Session ... does not belong to user` | Use the original `--user` or start a new session |
| `bound to a different workspace list` | Resume with the bound inputs or open a new session |
| First turn failed and the session cannot continue | Send a new message with a fresh `runId`. Without a checkpoint, `/api/v1/runs` uses the latest graph state. Cancellation aligns the next turn and the UI with the checkpoint |
| HTTP 409, `A run is already active for this session` | Wait or cancel the active turn. The session can still be read while it runs |
| `Run replay is not supported` | Send a new user message with a fresh run ID |
| `Extension reload failed` | Check `backend.log` for `stage=reload`, then correct the plugin, Hook, or MCP connection and retry the request. Invalid refreshes return HTTP 500 and keep the prior runtime |
| `Output directory is used by session ...` | Choose a nonoverlapping directory for the new session. See [output errors](sessions-and-outputs.md#common-errors) |
| `Output directory ... already contains files that no session owns` | Choose an empty directory or use a reviewed directory with explicit SDK `output_dir` |

<a name="empty-outputs"></a>

## Empty outputs

`/outputs` lists regular files in the bound output directory. The directory can be outside Home. Its location is recorded in `session_bindings` in Home's `sessions.sqlite`. The listing skips `artifacts/conversation_history`, `artifacts/large_tool_results`, symbolic links, and files written elsewhere. Permission errors surface as errors.

For a missing remote trace whose local file exists, see [Export traces over OTLP/HTTP](trace-export.md).
