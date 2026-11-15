# Quick start

This guide is for a first deploy. Formal mode has two paths. Both start the same control plane. Web and TUI are its two presentations:

| Path | Hosts | Entry |
| --- | --- | --- |
| **Recommended: one-click** | Ubuntu / Debian | `./deploy.sh` (config, dependencies, build including TUI, detached Web/API start, and health checks in one flow) |
| **Manual npm** | Windows, macOS, other Linux, or hand-edited env files | `npm install` → configure `.env` → `npm run build` → `npm run start` |

After deploy, put `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY` in the repository `.env` (`npm run start` loads it into the process environment) or in Home `~/.dataagent/.env`. Then ask from the Web UI or the TUI. Docker / Compose is **not** shipped, and the control plane has no registration.

## Requirements

- **One-click deploy**: Ubuntu or Debian (x86_64 / aarch64); Node.js 22 (the script can help install it after consent) and uv
- **Manual npm**: Linux, macOS, or Windows; Node.js >= 22, npm, and uv

Install and run the project in the same environment. On Windows, do not share `node_modules` between Windows and WSL.

## Recommended: Ubuntu / Debian one-click deploy

`./deploy.sh` does **not** support native Windows / macOS (use manual npm below).

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

On success Web + API keep running in the background (detached process group). Closing the terminal or pressing `Ctrl+C` in `./deploy.sh logs` does **not** stop DataGallery — use `./deploy.sh stop`. One-click deploy also builds the TUI, but the TUI does **not** stay running with the stack; start it in another terminal when needed (see “Start the TUI” below).

Open `http://127.0.0.1:3000/data-tasks` (if the Web port differs, use the URL the script prints and keep the `/data-tasks` path). The model comes from `config.json` and the environment, not from a profile created in the Web UI.

### Configuration rules

- First run: the script generates `.env`, then confirms ports / public URL. No model key is required at deploy time.
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

`LLM_*` is not required during deploy. Re-running deploy uses a maintenance window: stop the managed process group before `npm ci` and builds.

## Windows / macOS / other: manual npm deploy

`./deploy.sh` targets **Ubuntu / Debian only** and does not support native Windows / macOS. On Windows, macOS, or other distros, install, configure, and start with npm as below. Use the same path for hand-edited env files or split processes.

Formal environments:

| Environment | Use for |
| --- | --- |
| **Formal test** | Local / private acceptance |
| **Real production** | Public service, behind a reverse proxy |

### 1. Install dependencies

From the repository root:

```bash
node -v
npm install
```

`node -v` must report 22 or higher. uv must already be installed. Install syncs the control-plane Python dependencies; time depends on your machine and network.

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit the root `.env` and set the model. The Web UI cannot change it:

```bash
LLM_MODEL=qwen-plus
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=your-api-key
```

DeepSeek example:

```bash
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-api-key
```

### 3. Build and start (same for formal test and real production)

`npm run build` compiles the web app and TUI. `npm run start` starts the control plane when that address is down, serves the web app, and leaves the TUI ready for another terminal:

```bash
npm run build
npm run start
```

In another terminal:

```bash
npm run start:tui
```

Started separately, both the web app and the TUI start the control plane when it is down. The address comes from `config.json` `server.host` and `server.port`, default `http://127.0.0.1:8790`:

```bash
npm run start:api
npm run start:web
```

Checks:

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
```

Open [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks). The control plane has no registration or login.

## Run your first question

On `/data-tasks`, send a question. The model, plugins, and workspaces are the configuration the control plane loaded at startup.

Suggested prompt:

```text
In three sentences, say what you can do right now.
```

If a read-only workspace is configured, ask which files are in that directory. A streamed reply means the Web path to the control plane is working. Database questions only make sense after the matching plugin is enabled.

## Start the TUI

One-click deploy builds the TUI during the build stage, but does **not** auto-start it and does **not** treat it as a managed background service. Start it directly and let the TUI launch the backend:

```bash
npm run start:tui
```

The address comes from `config.json` `server.host` and `server.port`, default `http://127.0.0.1:8790`. When a control plane is already listening there, the same command attaches. `--runtime-url` only attaches and does not start a backend.

`./deploy.sh tui` also attaches when the deployed stack is already running. It uses the API address recorded at start.

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
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
```

If the health check fails:

```bash
./deploy.sh start
# manual path: npm run start
```

### Model unavailable

Symptom: Agent run reports provider, 401, rate limit, or model not found errors.

Fix:

- Check `LLM_API_KEY` in `.env`.
- Confirm `LLM_BASE_URL` matches your provider's compatible endpoint.
- Confirm `LLM_MODEL` is available on your account.
- Restart the control plane after editing. The Web UI has no model test route.

### Port conflict

Default ports:

| Service | Port |
| --- | --- |
| Web | 3000 |
| API | 8790 (`server.port` in `config.json`) |

Stop the conflicting process, or change `server.port` in `config.json`. `npm run start:web` reads that port.

### The model cannot read a local directory

- The control plane has no datasource registry. Put the directory in `dataagent.workspaces`, or pass `--workspace` at startup.
- The directory must exist before startup, and the process must be able to read and enter it.
- A new session writes that list into the `session_bindings` table in `sessions.sqlite`. After changing the list, start a new session.

## Next steps

- Use the Web UI: [Web workbench guide](guides/web-workbench.md)
- Use the terminal UI: [TUI guide](guides/tui.md)
- Give the model a local directory: [Where data comes from](guides/data-sources.md)
- Review capability boundaries: [Capabilities](capabilities.md)
