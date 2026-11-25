# DataAgent

A local data agent built with Python and Deep Agents, with Web, TUI, REST, A2A, and Python SDK entry points. The main agent can delegate to local plugins such as nl2sql and preserve conversation state, result files, and traces by session.

[简体中文](README_zh.md) · [Documentation](docs/en/README.md) · [Quick start](docs/en/quick-start.md)

## Current architecture and capabilities

```text
Web → Next same-origin proxy ─┐
TUI → AG-UI / REST ──────────┼→ dataagent serve → main agent → tools / local subagents / MCP
Other agents → A2A ─────────┘                           └→ nl2sql → configured database
Python SDK → the same configuration and agent assembly, without requiring HTTP
```

- **One backend, several entry points.** Web and TUI share one Python control plane. A2A 1.0 exposes the same runtime through JSON-RPC, or can run as a separate A2A-only process.
- **Sessions and outputs.** The service persists checkpoints, a session index, traces, and outputs. A2A, Web, and TUI can continue the same session; A2A's in-memory Tasks do not survive a restart.
- **Plugins and subagents.** Configuration enables plugins, Skills, Hooks, MCP, and compiled subagents. Database queries require an enabled and configured plugin; the control plane has no built-in datasource registration service.
- **File-based configuration.** Models, addresses, plugins, and inputs come from Home configuration and environment variables. Web does not create or test model profiles.
- **Correlated diagnostics.** Logs use `backend.log`, `web.log`, and `tui.log`. A2A request, task, and run IDs connect responses to logs and local traces.

The current backend has no registration/login, Web model management, datasource registration, knowledge-base management, conversation branching, or trace DAG API. Remaining legacy UI entries do not imply backend support. See [Capabilities](docs/en/capabilities.md).

## 🚀 Run It Locally

Requirements: Node.js ≥22.12.0 and uv; the backend uses Python 3.12. Install and run in the same environment. Do not share `node_modules` between Windows and WSL. This version has no Docker / Compose deployment.

### Linux one-click deployment

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

The script prepares configuration, dependencies, and builds, then starts Web/API in the background. Deployment does not require a model key, but conversations require a working model in the repository `.env` or Home `.env`; restart the service after configuring it. TUI is built but does not run in the background; open another terminal and run `./deploy.sh tui`.

The script has no distribution or CPU architecture allowlist. If Node.js needs installing, it selects `apt-get`, `dnf`, or `yum`, using NodeSource by default or a custom package through `DATAAGENT_NODE_PACKAGE_URL`. Unsupported installation environments and dependency failures stop with an error; existing Node.js ≥22.12.0 and npm do not require a package manager. See [deployment with custom mirrors](docs/en/guides/mirror-deployment.md) for tool provisioning, Python selection, package mirrors, and the model gateway.

```bash
./deploy.sh status
./deploy.sh doctor
./deploy.sh logs
./deploy.sh stop
./deploy.sh start
```

Closing a terminal or stopping log following does not stop the managed service. Use `./deploy.sh stop`. Redeployment stops the managed service before installation and builds; use `./deploy.sh deploy --reconfigure` to change ports.

### Manual installation, build, and startup

For native Windows, macOS, other Linux distributions, or manual configuration:

Before installation, ensure Node.js ≥22.12.0 and uv are on `PATH`. On restricted networks, select an available Python 3.12 interpreter and download sources using the [manual preparation steps](docs/en/quick-start.md#1-prepare-tools-and-download-sources) before running the installation commands below.

```bash
npm install --include=dev
uv sync --no-dev --extra video
cp .env.example .env
```

Edit the repository `.env`:

The backend machine must be able to reach the model endpoint. Replace the example address and model name with your service settings.

```bash
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

```bash
npm run build
npm run start
# Optional, in another terminal
npm run start:tui
```

Open `http://127.0.0.1:3000/data-tasks` directly, without registering or creating a Web model profile. Start by asking the agent to describe its capabilities. Database questions require the relevant plugin to be enabled and configured first.

A 200 from `/healthz` means the control plane is live; a 200 from `/ready` means conversation engine configuration loaded successfully. See [manual deployment validation](docs/en/quick-start.md#5-verify-health-and-model-calls).

## Configuration and independent services

Build the DataAgent Python package with `uv build --wheel --clear --out-dir dist/server`. See [Python package deployment](docs/en/guides/agentkit-package.md) for installation and Web/TUI connection. `make build` also creates the TUI tarball. Run `npm run smoke:agentkit-package` afterward to verify the packages.

Home defaults to `~/.dataagent`; set `DATAAGENT_HOME` in the process environment to change it. The CLI initializes missing configuration templates on first startup.

| File or setting | Purpose |
| --- | --- |
| `<Home>/config.yaml` | Product configuration with `schema_version: 4`, including `models`, `dataagent`, `plugins`, and `server` |
| `<Home>/.env` | Values for `$env{NAME}` references; nonempty process variables override the file |
| `--config` | Replaces Home configuration; does not select a neighboring `.env` |
| `--env-file` | Explicitly replaces Home `.env`, including for direct Python CLI launches |
| `server.host / port` | Backend defaults to `127.0.0.1:8790`; Web defaults to port 3000 |

Prepare the source environment with the required extras first; the commands below use it without synchronizing dependencies. See [staged installation](docs/en/guides/mirror-deployment.md#staged-installation).

```bash
# Backend, including A2A routes
uv run --no-sync dataagent serve

# A2A only: choose this OR serve, since one Home supports one backend
uv run --no-sync dataagent serve-a2a
```

`serve` provides `/healthz`, `/ready`, `/api/v1/runs`, sessions, and outputs. `serve-a2a` has none of those REST/health routes. SDK callers can use `prepare_runtime`, `create_agent`, and `ainvoke` directly; their host manages the checkpointer, exception handling, and logging configuration.

## A2A calls

The service exposes `GET /.well-known/agent-card.json` and `POST /a2a/jsonrpc`. Requests must include `A2A-Version: 1.0`. Calls can wait for the final result or use `returnImmediately: true` to obtain a Task for polling. A2A streaming is not supported; see [call modes and examples](docs/reference/a2a.md#等待结果与异步任务).

```bash
curl -sS http://127.0.0.1:8790/a2a/jsonrpc \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"request-1","method":"SendMessage","params":{"message":{"role":"ROLE_USER","messageId":"message-1","contextId":"thread-1","parts":[{"text":"Describe your capabilities in three sentences."}]}}}'
```

When `DATAAGENT_A2A_AUTH_TOKEN` is set, also send `Authorization: Bearer <token>`. The Agent Card remains public. Execution failures can still return HTTP 200; inspect Task state and `metadata.dataagent.error`. See [REST and A2A](docs/en/reference/rest-api.md) for query and cancellation contracts.

## Logs and diagnosis

`<Home>/runtime/logs/` uses three main files:

| File | Contents |
| --- | --- |
| `backend.log` | Backend, plugins, models, A2A, shared dependencies, and service lifecycle |
| `web.log` | Web builds, server, proxy, and reported browser errors |
| `tui.log` | TUI builds, connections, transport, and interaction |

Each rotates at 5,000,000 bytes with 3 backups. Correlate records using `X-Request-ID`, Task ID, and run IDs. A file may not exist until its process first writes a record. Browser reporting requires a reachable Web server and has no offline-storage guarantee.

## Security boundary

- The default listener is local. REST has no access token. The optional A2A shared token protects JSON-RPC only; it is not user login or tenant isolation.
- Known credentials are redacted before log and trace export. Conversations, SQL, tool results, and CSV files can still contain business data.
- Workspace read-only guidance and plugin SQL rules are not an operating-system sandbox. File tools and shell use backend process permissions; nl2sql SQL security rules must be enabled through configuration.

See [Security](docs/en/security.md) and [Supported inputs](docs/en/reference/supported-datasources.md).

## Documentation and development

- [Quick start](docs/en/quick-start.md): deployment, models, lifecycle, and troubleshooting.
- [Architecture](docs/en/architecture/overview.md): the control plane, Web/TUI, and A2A.
- [Configuration](docs/en/reference/configuration-api.md) and [API](docs/en/reference/rest-api.md): current integration contracts.
- [Contributing](CONTRIBUTING.md). Runtime, plugin, and SDK technical docs are under `docs/` in this repository.
- [Historical v0.2.0 release notes](docs/en/releases/v0.2.0.md): product design and capabilities at that time, rather than an acceptance checklist for the current refactored runtime.

```bash
npm run test:deploy
npm run test:web
npm run test:tui
make test
```

Apache-2.0; see [LICENSE](LICENSE).

Configuration files use YAML 1.2 (`config.yaml`, `.plugin.yaml`, `.mcp.yaml`).
