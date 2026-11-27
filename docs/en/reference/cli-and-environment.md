# CLI and environment

Source commands below use the project's prepared environment without syncing it. See [staged installation](../guides/deploy-mirrors.md#staged-installation). With the wheel installed, run `dataagent` from its environment.

## `dataagent serve` { #dataagent-serve }

`dataagent.cli:main` and `python -m dataagent` invoke the same implementation. Web and TUI use this backend.

```bash
uv run --no-sync dataagent serve \
  --config path/to/config.yaml \
  --user default \
  --session <threadId> \
  --env-file /path/.env
```

| Option | Default | Effect |
| --- | --- | --- |
| `--config` | None | Existing YAML to use instead of Home `config.yaml` |
| `--user` | `default` | Local user ID and `runtime/users/<user>/` partition |
| `--session` | None | Verify which user owns the session; the same thread resumes with its bound inputs |
| `--env-file` | None | Existing file to use instead of Home `.env` |

Configure `server.host` and `server.port` in YAML. The default is `127.0.0.1:8790`. `npm run start`, the Web proxy, and argument-free TUI read the selected file or the Home configuration. TUI attaches if the control plane answers or spawns it otherwise. `npm run start -- --config <path>` and `./deploy.sh start --config <path>` pass that path to the backend. The npm launcher accepts only `--config`, `--web-host`, `--web-port`, `--api`, and `--web`. Run `dataagent serve` directly for options such as `--env-file` and `--user`. Deploy records the resolved address for `tui`, `status`, and `doctor`.

Missing, non-file, or malformed explicit YAML causes both servers to exit 1 before listening. Stderr and startup JSON in `backend.log` include `component=dataagent.startup`, `stage`, and diagnostics. If YAML loads but preparation fails, `serve` may listen with `/healthz` at 200, `data.conversation.ready: false`, and `/ready` at 503. Standalone A2A lacks those health routes. Set required model variables before restarting. Non-default models resolve variables at first use.

| Internal option | Effect |
| --- | --- |
| `--reload` | Watch `dataagent/` and restart for development. npm and deploy launchers omit it |
| `--stdio-ready` | Hidden handshake for a backend spawned by TUI, used with process-only `DATAAGENT_INSTANCE_ID` |

## `dataagent serve-a2a` { #dataagent-serve-a2a }

Expose the same runtime as a standalone A2A 1.0 service. Configuration, user, and environment options work as in `serve`. YAML supplies inputs. Each `contextId` is its own session, so this command has no `--session`.

```bash
uv run --no-sync dataagent serve-a2a \
  --config path/to/config.yaml \
  --user default \
  --env-file /path/.env \
  --host 127.0.0.1 \
  --port 8791
```

| Option | Default | Effect |
| --- | --- | --- |
| `--host` | `server.host` | Bind address. For remote access use a reachable interface; clients use its IP or hostname |
| `--port` | `server.port` | Bind port. Startup fails if occupied |
| `--auth-token` | `DATAAGENT_A2A_AUTH_TOKEN` | Require `Authorization: Bearer <token>` on JSON-RPC. Prefer the variable to keep it out of process arguments |

JSON-RPC uses `/a2a/jsonrpc` and requires `A2A-Version: 1.0`. Default access is anonymous for the shared team environment. The two commands cannot simultaneously own the same Home because they share `runtime/backend.lock`. Regular `serve` also exposes these A2A routes. See [A2A](a2a.md).

SDK `LaunchOptions` additionally accepts `cwd` and `init_home`. CLI startup initializes Home by default. The SDK defaults to `init_home=False`. YAML `dataagent.input_workspaces` supplies default inputs.

## `dataagent history-sql` { #dataagent-history-sql }

Download historical SQL from a configured semantic service for de-agent's `history_sql` corpus.

```bash
uv run --no-sync dataagent history-sql --config path/to/config.yaml
```

| Option | Default | Effect |
| --- | --- | --- |
| `--config`, `--env-file` | None | Same as `serve` |
| `--output-dir` | See precedence | Absolute corpus directory |
| `--compare-dir` | None | Compare with an existing corpus and write `comparison` to the report |
| `--page-size` | 200 | Entries per page |
| `--if-enabled` | Off | Exit 0 when `plugins.config.de-agent.history_sql.enabled` is false |
| `--no-lock` | Off | Leave the new snapshot writable |

The first provided value wins: `--output-dir`, `DATAAGENT_HISTORY_SQL_CORPUS`, `history_sql.corpus_path`, then `<home>/history_sql`. Success prints JSON to stdout and exits 0. Configuration errors exit 2, and download errors exit 1.

## TUI

Installed `dataagent-cli` and `da` connect to a running Server. The source `npm run start:tui` can start a local backend. See [package deployment](../guides/deploy-packages.md#connect-tui).

| Source option | Effect |
| --- | --- |
| No options | Attach to configured `server.host` and `server.port` or start there if absent |
| `--config`, `--env-file`, `--user`, `--session` | Forward to a newly spawned backend even if a server already listens |
| `--resume [threadId]` | Resume at startup |
| `--runtime-url` | Attach only. Incompatible with local spawn options |

## Environment variables { #environment-variables }

See repository `.env.example`. Default model configuration needs the three `LLM_*` values. Use the relevant launch process for process-only settings.

| Variable | Read by | Effect |
| --- | --- | --- |
| `DATAAGENT_HOME` | Launch process | Home, default `~/.dataagent` when unset or blank. Expand `~` and resolve relative paths from startup cwd. The backend, launchers, and TUI use the same rule. Backend env files ignore it and warn |
| `UV_PROJECT_ENVIRONMENT` | Source launcher | Prepared virtual environment, default repository `.venv` |
| `NO_COLOR` | TUI process | A nonempty value disables color |
| `DATAAGENT_TUI_INCREMENTAL_RENDERING`, `DATAAGENT_TUI_OPTIMIZE_ERASE_LINES`, `DATAAGENT_TUI_WHEEL_LINES_PER_TICK` | TUI process | Use 0 to disable incremental drawing, 1 to enable erase-line optimization, or set lines per wheel event |
| `API_PROXY_TARGET` | Web launcher | In Web-only mode explicit `--config` supplies Server addresses with defaults for missing fields. Otherwise use this variable, Home YAML, then default, in that order. The Web bind uses `--web-host` and `--web-port` |
| `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY` | Config `$env{...}` references | Template variable names. Other names work if configured |
| `DATAAGENT_OTLP_TRACES_ENDPOINT` | Backend process | Full real-time OTLP/HTTP trace URL. Unset means local trace files only |
| `DATAAGENT_OTLP_HEADERS` | Backend process | Comma-separated `key=value` export headers |
| `DATAAGENT_A2A_AUTH_TOKEN` | Backend effective environment | A2A Bearer token for both servers. Redacted from logs |
| `DATAAGENT_WEB_ORIGIN` | Web process | Allowed browser error-reporting origin, such as `https://data.example.com`. Separate from the backend proxy target |
| `DATAAGENT_HISTORY_SQL_CORPUS` | Backend process | Absolute SQL corpus path, ahead of `history_sql.corpus_path` |

`$env{NAME}` must match `[A-Za-z_][A-Za-z0-9_]*`. Process-only fields and plugin-owned variables such as `DATAOPS_*` come from the launch environment. The npm or deployment launcher can load repository-root `.env` into that environment. Export `DATAAGENT_HOME` before startup.

The backend reads exactly one env file, explicit `--env-file` or Home `.env`. The explicit file must exist. `--config` does not affect selection, and no files beside the YAML, in input directories, or in cwd are read automatically by the backend. Nonempty process values override file values, and empty values do not enter the effective environment. Loading the file does not mutate `os.environ` or configure other terminals.

Logs are under Home `runtime/logs/`. Backend logging defaults to INFO. There is no global `LOG_LEVEL` setting. Launcher-managed `DATAAGENT_LOG_CAPTURE` avoids duplicate writes. See [observability](../../development/architecture/observability.md).
