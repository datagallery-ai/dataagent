# Quick start

This guide is for a first deploy. Formal mode has two paths. Both start the same control plane. Web and TUI are its two presentations:

| Path | Hosts | Entry |
| --- | --- | --- |
| **Recommended: one-click** | Linux (including openEuler) | `./deploy.sh` (config, dependencies, build including TUI, detached Web/API start, and health checks in one flow) |
| **Manual npm** | Windows, macOS, or hand-edited env files | Prepare tools and download sources → `npm install` + `uv sync` → configure model → `npm run build` → `npm run start` |

After deploy, put `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY` in the repository `.env` (`npm run start` loads it into the process environment) or in Home `~/.dataagent/.env`. Then ask from the Web UI or the TUI. Docker / Compose is **not** shipped, and the control plane has no registration.

## Requirements

- **One-click deploy**: Linux; Node.js ≥22.12.0, npm, and uv. If Node.js is missing or too old, the script can install it through `apt-get`, `dnf`, or `yum` after consent
- **Manual npm**: Linux, macOS, or Windows; Node.js >= 22.12.0, npm, and uv

The backend uses Python 3.12. On restricted networks, prepare an interpreter and reachable download sources before installation. See the manual steps below and [deployment with custom mirrors](guides/mirror-deployment.md).

The minimum version reflects the locked TUI and build/test dependencies.

Install and run the project in the same environment. On Windows, do not share `node_modules` between Windows and WSL.

## Recommended: Linux one-click deploy

`./deploy.sh` does not reject distributions or CPU architectures in advance. Node.js installation uses NodeSource by default, or the package specified by `DATAAGENT_NODE_PACKAGE_URL`: DEB packages through `apt-get`, or RPM packages through `dnf` / `yum`. If no supported package manager is available, it asks you to install Node.js manually. Incompatible dependencies, insufficient permissions, or installation failures stop with an error. See [deployment with custom mirrors](guides/mirror-deployment.md) for mirror and interpreter configuration. Manual npm is recommended on Windows / macOS.

```bash
git clone https://github.com/datagallery-lab/datafoundry.git
cd datafoundry
./deploy.sh
```

On success Web + API keep running in the background (detached process group). Closing the terminal or pressing `Ctrl+C` in `./deploy.sh logs` does **not** stop DataGallery — use `./deploy.sh stop`. One-click deploy also builds the TUI, but the TUI does **not** stay running with the stack; start it in another terminal when needed (see “Start the TUI” below).

Open `http://127.0.0.1:3000/data-tasks` (if the Web port differs, use the URL the script prints and keep the `/data-tasks` path). The model comes from `config.yaml` and the environment, not from a profile created in the Web UI.

### Configuration rules

- First run: the script generates `.env`, then confirms ports / client-reachable URL. No model key is required at deploy time.
- Later interactive `./deploy.sh` / `./deploy.sh deploy` skips configuration questions if the managed service is running or the root `.env` is non-empty.
- To change ports or the client-reachable URL again (existing secrets are kept; `.env` is backed up first):

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
./deploy.sh restart   # validate and reuse the running deployment's settings
./deploy.sh logs      # follow runtime logs; Ctrl+C does not stop the stack
./deploy.sh doctor    # read-only dependency / config / port / disk / health checks
./deploy.sh tui       # optional: foreground TUI client (API must be healthy; not a managed service)
./deploy.sh help
```

`deploy`, `restart`, and `doctor` prefer explicit `--config`, `--web-host`, and `--web-port` values, then reuse the config file and Web address of a running managed deployment, then fall back to Home configuration and the default Web address. An explicit Web port is checked directly and a conflict is reported. Interactive deployment only prompts for ports that were left unspecified.

When changing to an overlapping listen address on the same port, Linux uses `ss` to confirm that the listener belongs to the managed service. If another listener conflicts or ownership cannot be verified, the current service keeps running and the command reports the conflict.

`deploy` prepares dependencies and builds. `start` / `restart` use the existing Python environment and build artifacts; missing prerequisites direct you to run `deploy` again. Managed startup does not install dependencies or build missing artifacts. Manual `npm start` also uses the existing environment and Web build.

`deploy` automatically checks dependencies, deployment directory permissions, configuration, and the selected listen addresses. Run `./deploy.sh doctor` to inspect mirror connectivity, disk space, or the current service status.

`restart` validates the new configuration with the Server's configuration loader before stopping the running service. `doctor` performs the same validation when the Python environment exists. Otherwise, it checks the listen settings and reports that Server configuration validation was skipped.

`LLM_*` is not required during deploy. Re-running deploy uses a maintenance window: stop the managed process group before `npm install` and builds.

### Log files

Deployment and independent Web/API/TUI launches use `<Home>/runtime/logs/`. `DATAAGENT_HOME` selects Home; the default is `~/.dataagent`.

- `backend.log`: backend, plugins, shared environment preparation and service lifecycle.
- `web.log`: Web builds, server diagnostics and reported browser errors.
- `tui.log`: TUI builds, startup, connections and interaction errors.

`./deploy.sh logs` follows all three files. Logs redact credentials before writing and retain diagnostic stacks. Each file rotates at 5,000,000 bytes with 3 backups. Browser reporting requires a reachable Web server and does not provide offline storage.

When serving Web through a reverse proxy, domain name, or wildcard listen address, set server-side `DATAAGENT_WEB_ORIGIN` to the browser-facing origin (for example, `https://data.example.com`) to validate browser error reports. Each Web process accepts at most 60 reports and 1 MiB of report bodies per minute, shared across all sources.

## Windows / macOS / other: manual npm deploy

On Windows, macOS, or Linux with manually prepared dependencies, install, configure, and start with npm as below. Use the same path for hand-edited env files or split processes.

Formal environments:

| Environment | Use for |
| --- | --- |
| **Formal test** | Local / deployment-environment acceptance |
| **Production** | Persistent service in the deployment environment; use a domain name and a reverse proxy as required |

### 1. Prepare tools and download sources

Obtain Node.js ≥22.12.0 and uv for your OS and architecture from a reachable source, install them, and add their executable directories to `PATH`. Check existing tools from the repository root:

```bash
node --version
npm --version
uv --version
```

On restricted networks, preinstall Python 3.12, select its executable, and disable automatic downloads so a missing interpreter fails immediately. Replace this path and run in the same shell as `uv sync`:

```bash
export UV_PYTHON=/opt/python3.12/bin/python3.12
export UV_PYTHON_DOWNLOADS=never
uv python find "$UV_PYTHON" --no-python-downloads --show-version
```

When mirrors are needed, configure these process variables before installation, replacing the example addresses:

```bash
export npm_config_registry="https://mirror.example/npm/"
export UV_DEFAULT_INDEX="https://mirror.example/pypi/simple/"
npm config get registry
```

In PowerShell, set variables with `$env:NAME="value"` and use an interpreter path such as `C:\Python312\python.exe`; replace `$UV_PYTHON` with `$env:UV_PYTHON` in the check command. If Python must be downloaded, configure `UV_PYTHON_INSTALL_MIRROR` and allow downloads as described in the [mirror guide](guides/mirror-deployment.md).

`npm_config_registry` selects the frontend registry; `UV_DEFAULT_INDEX` selects the Python runtime/build dependency index. These process variables do not require global registry changes. For manual installation, configure download sources in the shell or through npm and uv configuration.

Run `uv sync` to resolve and install Python dependencies from the configured index according to `pyproject.toml`. The repository retains `uv.lock` as a resolution snapshot to reduce initial optional-dependency metadata downloads. Installation uses neither `--frozen` nor `--locked`; uv updates the snapshot when the configuration or index changes. No `sed` or manual URL rewrite is required, and transitive versions need not be identical across environments.

### 2. Install dependencies

```bash
npm install --include=dev
uv sync --no-dev --extra video
```

The first command installs Web and TUI dependencies. The second prepares `.venv` with runtime and video dependencies. Use `uv sync` for development tools, or choose the extras you need. See [staged installation](guides/mirror-deployment.md#staged-installation).

### 3. Configure the model service

```bash
cp .env.example .env
```

Edit the root `.env` for a compatible model service reachable from the deployment machine. Replace the example address, model name, and credential with your service settings. The Web UI cannot change them:

```bash
LLM_MODEL=your-model-name
LLM_BASE_URL=https://your-openai-compatible-host/v1
LLM_API_KEY=replace-with-your-key
```

The model endpoint is a runtime setting, configured separately from npm / Python download sources. Services and pages can start before the model is configured. Complete model configuration before conversations and restart after changes.

### 4. Build and start (same for formal test and real production)

`npm run build` compiles the web app and TUI. `npm run start` starts the control plane when that address is down, serves the web app, and leaves the TUI ready for another terminal:

```bash
npm run build
npm run start
```

In another terminal:

```bash
npm run start:tui
```

To run the services in separate terminals, start the API first. Web connects to the address in `config.yaml` (default `http://127.0.0.1:8790`). For a separately deployed backend, set `API_PROXY_TARGET` before starting Web.

```bash
uv run --no-sync dataagent serve
npm run start:web
```

If model variables are in the repository root `.env`, start the API with `uv run --no-sync --env-file .env dataagent serve`.

### 5. Verify health and model calls

Check from another terminal, adjusting ports to match the actual configuration:

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
curl -I http://127.0.0.1:3000/data-tasks
```

`/healthz` should return 200 for a live control plane. `/ready` returns 200 when conversation engine configuration loads successfully, or 503 when the model is not configured; check the model settings and backend logs. A successful page response establishes page availability. Follow the next section to send a question and verify an actual model response.

Open [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks). The control plane has no registration or login.

## Run your first question

On `/data-tasks`, send a question. The model, plugins, and workspaces are the configuration the control plane loaded at startup.

Suggested prompt:

```text
In three sentences, say what you can do right now.
```

If a read-only workspace is configured, ask which files are in that directory. A streamed reply means the Web path to the control plane is working. Database questions only make sense after the matching plugin is enabled.

## Start the TUI

When the TUI starts its own backend, it loads the repository root `.env`, like `npm start`. Existing terminal variables take precedence. Nonempty model values loaded from the project file enter the process environment and override the same variables from the backend Home `.env`. Different `LLM_MODEL` or `LLM_BASE_URL` values in these files can therefore change the selected model. Attaching with `--runtime-url` does not reload an existing backend configuration.

One-click deploy builds the TUI during the build stage, but does **not** auto-start it and does **not** treat it as a managed background service. Start it directly and let the TUI launch the backend:

```bash
npm run start:tui
```

The address comes from `config.yaml` `server.host` and `server.port`, default `http://127.0.0.1:8790`. When a control plane is already listening there, the same command attaches. `--runtime-url` only attaches and does not start a backend.

`./deploy.sh tui` uses the API address recorded at managed startup by default. Pass `--runtime-url` to select an existing backend.

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

Upgrade to Node.js 22.12.0 or higher, then retry. One-click deploy can also install Node after consent; on the manual path, re-run `npm install`.

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
| API | 8790 (`server.port` in `config.yaml`) |

Stop the conflicting process, or change `server.port` in `config.yaml`. `npm run start:web` reads that port.

### The model cannot read a local directory

- The control plane has no datasource registry. Put the directory in `dataagent.workspaces`, or pass `--workspace` at startup.
- The directory must exist before startup, and the process must be able to read and enter it.
- A new session writes that list into the `session_bindings` table in `sessions.sqlite`. After changing the list, start a new session.

## Next steps

- Use the Web UI: [Web workbench guide](guides/web-workbench.md)
- Use the terminal UI: [TUI guide](guides/tui.md)
- Give the model a local directory: [Where data comes from](guides/data-sources.md)
- Review capability boundaries: [Capabilities](capabilities.md)
