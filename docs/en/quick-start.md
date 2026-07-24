# Quick start

This guide is for first-time DataFoundry deployers. Formal mode has two paths; **both** start the Web workbench with `password` auth (do **not** run `npm run dev`):

| Path | Hosts | Entry |
| --- | --- | --- |
| **Recommended: one-click** | Ubuntu / Debian | `./deploy.sh` (config, dependencies, build including TUI, detached Web/API start, and health checks in one flow) |
| **Manual npm** | Windows, macOS, other Linux, or hand-edited env files | `npm install` → configure `.env` → `npm run build` / `build:web` → `npm run start` |

After deploy, configure a model in the Web UI and run an analysis against the built-in DTC Growth Review data source. Docker / Compose is **not** shipped in this release.

## Requirements

- **One-click deploy**: Ubuntu or Debian (x86_64 / aarch64); Node.js 22 (the script can help install it after consent)
- **Manual npm**: Linux, macOS, or Windows; Node.js >= 22 and npm
- Optional external [Data Link](https://github.com/datagallery-lab/datalink): run it as a separate process if you want semantic graph features (not required for deploy)

Install and run the project in the same environment. On Windows, do not share `node_modules` between Windows and WSL.

## Recommended: Ubuntu / Debian one-click deploy

`./deploy.sh` does **not** support native Windows / macOS (use manual npm below).

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

On success Web + API keep running in the background (detached process group). Closing the terminal or pressing `Ctrl+C` in `./deploy.sh logs` does **not** stop DataFoundry — use `./deploy.sh stop`. One-click deploy also builds the TUI, but the TUI does **not** stay running with the stack; start it in another terminal when needed (see “Start the TUI” below).

Open `http://127.0.0.1:3000/login` (or the Web URL printed by the script if the port differs), register and sign in, create/test/enable an OpenAI-compatible model profile, then go to `/data-tasks`.

### Configuration rules

- First run: the script generates `.env` and `apps/web/.env.local`, then confirms ports / public URL. No model key is required at deploy time.
- Later interactive `./deploy.sh` / `./deploy.sh deploy`: if a complete `.env` already exists, configuration questions are skipped.
- To change ports or the public URL again (existing secrets are kept; `.env` is backed up first):

```bash
./deploy.sh deploy --reconfigure
```

- Unattended / CI defaults (no prompts; fails immediately on port conflicts or install that needs a sudo password):

```bash
./deploy.sh deploy --non-interactive
```

`--reconfigure` and `--non-interactive` are mutually exclusive and only valid with `deploy`.

### Lifecycle commands

```bash
./deploy.sh status    # process + API / Web health
./deploy.sh start     # start an existing build (no install/build)
./deploy.sh stop      # stop only the managed process group
./deploy.sh restart   # stop then start (no install/build)
./deploy.sh logs      # follow runtime logs; Ctrl+C does not stop the stack
./deploy.sh doctor    # read-only dependency / config / port / disk / health checks
./deploy.sh tui       # optional: foreground TUI client (API must be healthy; not a managed service)
./deploy.sh help
```

`LLM_*` is not required during deploy. Set `AUTH_PUBLIC_BASE_URL` for remote hosts. Re-running deploy uses a maintenance window: stop the managed process group before `npm ci` and builds.

### External Data Link (optional)

One-click deploy starts **only** Web + API. It does **not** install, start, or health-check Data Link.

To use semantic features, run [Data Link](https://github.com/datagallery-lab/datalink) as a separate service (typically MCP on `:8080` and REST on `:8081`), then in the Web workbench MCP settings add an external server, for example:

| Field | Example |
| --- | --- |
| `serverUrl` | `http://127.0.0.1:8080/mcp` |
| `apiUrl` | `http://127.0.0.1:8081` |
| `transport` | `streamable-http` |
| `toolManifest` | `[{ "name": "datalink_explore" }]` |

Use a name/id containing `datalink` (or `datagraph`) so the Data Link panel can recognize it.

## Windows / macOS / other: manual npm deploy

`./deploy.sh` targets **Ubuntu / Debian only** and does not support native Windows / macOS. On Windows, macOS, or other distros, install, configure, and start with npm as below. Use the same path for hand-edited env files or split processes. Do **not** run `npm run dev` in formal environments. Contributor hot-reload is in the appendix.

Formal environments:

| Environment | Use for | `AUTH_EMAIL_DELIVERY` | `AUTH_PUBLIC_BASE_URL` |
| --- | --- | --- | --- |
| **Formal test** | Local / private acceptance | `test` (verification links go to the API console) | e.g. `http://127.0.0.1:3000` |
| **Real production** | Public service | `smtp` (real email) | Public HTTPS origin |

Do **not** run `npm run dev` / `dev:api` / `dev:web` in either formal environment. Contributor hot-reload is in the appendix.

### 1. Install dependencies

From the repository root:

```bash
node -v
npm install
```

`node -v` must report 22 or higher. The first install generates the local DTC Growth Review SQLite fixture and compiles workspace dependencies; time depends on your machine and network.

### 2. Configure environment variables

```bash
cp .env.example .env
cp apps/web/.env.example apps/web/.env.local
```

#### 2.1 Model (optional server defaults)

Edit the root `.env` for optional server-default models (you can also configure models only in the Web UI):

```bash
LLM_PROVIDER=openai-compatible
LLM_MODEL=qwen-plus
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=your-api-key
```

DeepSeek example:

```bash
LLM_PROVIDER=openai-compatible
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-api-key
```

### 2.2 Formal test (recommended for first acceptance)

Root `.env`:

```bash
DATAFOUNDRY_AUTH_MODE=password
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000
AUTH_EMAIL_DELIVERY=test
AUTH_EMAIL_FROM=DataFoundry <no-reply@example.com>
# SMTP settings can stay empty for now
```

`apps/web/.env.local` (baked in at `next build`):

```bash
NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=password
# Leave empty so the browser uses the same-origin BFF (Cookie + CSRF)
NEXT_PUBLIC_AGENT_RUNTIME_URL=
NEXT_PUBLIC_CONFIG_API_URL=
API_PROXY_TARGET=http://127.0.0.1:8787
```

On register / password reset, copy the verification link from the **API process console**.

### 2.3 Real production

Start from the formal-test settings, then change to:

```bash
DATAFOUNDRY_AUTH_MODE=password
AUTH_SESSION_SECRET=replace-with-at-least-32-random-characters
AUTH_PUBLIC_BASE_URL=https://datafoundry.example.com
AUTH_EMAIL_DELIVERY=smtp
AUTH_EMAIL_FROM=DataFoundry <no-reply@example.com>
AUTH_SMTP_HOST=smtp.example.com
AUTH_SMTP_PORT=587
AUTH_SMTP_SECURE=false
AUTH_SMTP_USER=
AUTH_SMTP_PASSWORD=
```

Keep the frontend on `password`, empty public API URLs, and `API_PROXY_TARGET`. Put a reverse proxy in front; see [`deploy/nginx.datafoundry.conf.example`](https://github.com/datagallery-lab/datafoundry/blob/main/deploy/nginx.datafoundry.conf.example) — compress static assets; keep `/api/copilotkit` uncompressed and unbuffered for SSE.

### 3. Build and start (same for formal test and real production)

```bash
npm run build
npm run build:web
npm run start:api    # :8787
npm run start:web    # :3000
```

Checks:

```bash
curl http://127.0.0.1:8787/healthz   # process up
curl http://127.0.0.1:8787/ready     # Mastra / builtins ready (includes startup_ms)
```

Open [http://127.0.0.1:3000/login](http://127.0.0.1:3000/login) (or your public origin in real production), register or sign in, then go to `/data-tasks`.

After changing any `NEXT_PUBLIC_*` value in `apps/web/.env.local`, run `npm run build:web` again.

## Run your first question

On `/data-tasks`:

1. Click **New data task**.
2. Select the built-in **DTC Growth Review** data source.
3. Select **Server default** or your configured model next to the input box.
4. Send your first question.

Suggested prompt:

```text
Show me the tables in this datasource and explain the main fields of each.
```

Aggregation prompt:

```text
Compare GMV, gross margin, ad spend, and refunds by channel. Explain which channel should receive the next budget increment.
```

When you see schema inspection, SQL execution, and result output, the path is working.

## Start the TUI

One-click deploy builds the TUI during the build stage, but does **not** auto-start it and does **not** treat it as a managed background service. With the API running, start the foreground client in another terminal:

```bash
./deploy.sh tui
# or: npm run start:tui
```

Optionally point at the deployed API URL (defaults to `API_PORT` from `.env`):

```bash
./deploy.sh tui --runtime-url http://127.0.0.1:8787/api/copilotkit
```

Sign-in needs a running API and a password account (offline demo mode was removed):

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8787/api/copilotkit
```

Resume the latest server session:

```bash
npm run start:tui -- --resume
```

More commands: [TUI guide](guides/tui.md).

## Troubleshooting

For one-click deploy, start with:

```bash
./deploy.sh status
./deploy.sh doctor
./deploy.sh logs
```

On the manual npm path, confirm `npm run start` is still running and check that terminal's output.

### Wrong Node version

Symptom: `npm install` or build fails on Node version.

Fix:

```bash
node -v
# one-click:
./deploy.sh doctor
```

Upgrade to Node.js 22 or higher, then retry. One-click deploy can also install Node after consent; on the manual path, re-run `npm install`.

### Page does not load

Symptom: Browser cannot open the workbench URL.

Fix:

- Run `./deploy.sh status` (or confirm `npm run start` is still running on the manual path). Do not use `dev` in formal mode.
- Check whether port 3000 is in use; if the deploy script chose another port, use the URL it printed.
- If the process is stopped: `./deploy.sh start`.

### Backend not running

Symptom: Page loads but questions get no response, or the resource panel fails to load.

Fix:

```bash
./deploy.sh status
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/ready
```

If the health check fails:

```bash
./deploy.sh start
# manual path: npm run start
```

### No verification email

- **Formal test** (`AUTH_EMAIL_DELIVERY=test`): copy the link from the `start:api` terminal.
- **Real production** (`smtp`): check `AUTH_SMTP_*` and that `AUTH_PUBLIC_BASE_URL` matches the public origin.

### Model unavailable

Symptom: Agent run reports provider, 401, rate limit, or model not found errors.

Fix:

- Check `LLM_API_KEY` in `.env`.
- Confirm `LLM_BASE_URL` matches your provider's compatible endpoint.
- Confirm `LLM_MODEL` is available on your account.
- Run the test action in the Web workbench model configuration.

### Port conflict

Default ports:

| Service | Port |
| --- | --- |
| Web | 3000 |
| API | 8787 |

Stop the conflicting process, or use the port shown in the terminal. After changing the API port, update `API_PROXY_TARGET`.

### Database connection failed

- Server databases such as PostgreSQL / MySQL must be reachable.
- SQLite, CSV, Excel, and DuckDB files must use paths the API process can read.
- Prefer a read-only account or a test database for the first connection.
- Credentials are submitted only on create/update; read APIs do not return plaintext secrets.

## Appendix: contributor hot-reload (not formal mode)

For local code changes with hot reload only — **not** formal test or real production. Pick one stack; never mix with `start:*`.

```bash
# Root .env
DATAFOUNDRY_AUTH_MODE=dev

# apps/web/.env.local
NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=dev
NEXT_PUBLIC_AGENT_RUNTIME_URL=http://127.0.0.1:8787/api/copilotkit

npm run dev
# or: npm run dev:api && npm run dev:web
```

## Next steps

- Use the Web UI: [Web workbench guide](guides/web-workbench.md)
- Use the terminal UI: [TUI guide](guides/tui.md)
- Connect your own data: [Data sources guide](guides/data-sources.md)
- Review capability boundaries: [Capabilities](capabilities.md)
