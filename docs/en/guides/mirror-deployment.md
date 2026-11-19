# Linux deployment with custom mirrors

`./deploy.sh deploy` installs project dependencies, builds, and starts services. `start` and `restart` use existing environments and artifacts without installing dependencies. Mirror addresses belong to the deployment environment; do not commit deployment-specific addresses or a rewritten lockfile.

## Prepare tools

Provide Node.js ≥22.12.0, npm, uv, and Python 3.12. Existing tools on `PATH` are reused. Ensure configured apt / dnf / yum repositories are reachable when installing OS dependencies.

- **Node**: preferably preinstall it. Alternatively, export `DATAAGENT_NODE_PACKAGE_URL` pointing to a `.deb` or `.rpm` containing Node and npm for the target system and architecture. The existing package manager installs this file without configuring NodeSource; OS dependencies still use system repositories. Without this variable, automatic installation uses public NodeSource. Extract official tar archives and add their executables to `PATH` before deployment.
- **uv**: preinstall it from a mirror of the official archive, then place `uv` (and optionally `uvx`) on `PATH`, for example in `$HOME/.local/bin`. Export `PATH="$HOME/.local/bin:$PATH"`. Alternatively use a PyPI mirror following the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/). Both the installer and any artifacts it downloads must be reachable from the deployment environment; deployment does not add another uv installer.
- **Python**: set `UV_PYTHON=/opt/python3.12/bin/python3.12` for an existing interpreter. `UV_PYTHON=3.12` delegates local discovery to uv; Makefile still accepts the `PYTHON_VERSION=3.12` argument.

To require an existing interpreter without downloads:

```bash
export UV_PYTHON=/opt/python3.12/bin/python3.12
export UV_PYTHON_DOWNLOADS=never
```

If Python must be downloaded instead, configure uv's `UV_PYTHON_INSTALL_MIRROR` and leave downloads enabled. Preserve upstream dated directories and filenames as described in the [uv environment reference](https://docs.astral.sh/uv/reference/environment/).

## Export installation settings

Replace these example addresses and export them in the deployment shell:

```bash
# Only needed when deployment must install Node/npm. Use a .deb on Debian systems.
export DATAAGENT_NODE_PACKAGE_URL="https://mirror.example/node/node22.rpm"
export npm_config_registry="https://mirror.example/npm/"
export UV_DEFAULT_INDEX="https://mirror.example/pypi/simple/"
```

Node bootstrap and npm download settings use **process environment variables** or native tool configuration; the repository root `.env` alone does not configure them. Python preparation reads defaults from the repository root `.env`, with process variables taking precedence. Home `.env` is only for backend runtime configuration. Persist exports in the deployment user's shell configuration or deployment platform. npm registry configuration in `.npmrc` also works. Reuse npm / uv authentication, CA, and proxy settings.

The install hook delegates to native `uv sync`, resolving and installing through uv's standard index according to `pyproject.toml`. The repository retains `uv.lock` as a resolution snapshot, avoiding a large source download when initially resolving the unselected Spark extra. Installation uses neither `--frozen` nor `--locked`. No temporary requirements export, manual lockfile rewrite, or project copy is needed. Sync normally after changing the index; uv updates the snapshot. A new index may require resolving every optional dependency even when unselected, so its metadata and artifacts must also be available. The index must provide packages and build tools satisfying the project constraints; all transitive versions need not be identical across environments.

The default environment is `runtime/agentkit/.venv`. If uv's native `UV_PROJECT_ENVIRONMENT` is set, installation and startup use the same location; relative paths are resolved against `runtime/agentkit`. Existing tool authentication, CA, and proxy configuration continues to apply.

Python preparation follows the exact synchronization semantics of `uv sync`: install the selected dependencies and remove unselected packages. Development dependencies are included by default; extras are not.

| Purpose | Project entrypoint | Equivalent native selection |
| --- | --- | --- |
| Default development environment | `npm run prepare:python` | `uv sync --project runtime/agentkit` |
| Runtime dependencies only | `npm run prepare:python -- --no-dev` | `uv sync --project runtime/agentkit --no-dev` |

Repeat `--extra <name>` for optional capabilities declared by the project. Specify required extras on every synchronization; their dependencies are removed when no longer selected. The `npm install` / `npm ci` hook and `npm start` preparation for a missing environment explicitly select `--no-dev` without extras. Prepare optional capabilities after installation and before starting the service. Runtime and tests share the project environment by default, and test preparation includes the development group; changing the selection changes that environment.

Launchers use the prepared interpreter. When launching directly with uv, add `--no-sync` and prepare the required dependencies explicitly after changes.

Makefile preserves `PYTHON_VERSION`, `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`, and `NPM_REGISTRY` as compatibility arguments mapped to native tool settings. Existing native settings take precedence. Calls to `make prepare`, `make build`, `make ut`, and `make st` remain valid; tests prepare development dependencies through the shared entrypoint without repeated implicit synchronization in each suite. When the environment is ready, use `make ut UV_RUN_OPTS=--no-sync` or `npm run test:product -- --no-sync` to skip preparation; the same applies to `make st`.

<a id="staged-installation"></a>

## Staged installation and native commands

From the repository root, configure the interpreter and download sources in the same shell as above, then run each stage:

```bash
# Skip Python preparation in the root install hook to observe each stage separately.
SKIP_POSTINSTALL_BUILD=1 npm ci
uv sync --project runtime/agentkit --no-dev
npm run build
```

The second command can also be `npm run prepare:python -- --no-dev`. Direct uv commands require the relevant environment variables to be exported first; they do not additionally load the root `.env`. Add `--extra video` when video is needed; use `--extra all` for Spark/Hive and video after preparing the required external environment. Synchronizing an existing environment also removes unselected dependencies.

Create and fill in the repository root `.env` using the [configuration instructions](../quick-start.md#3-configure-the-model-service), then select that file explicitly when starting the prepared source backend:

```bash
uv run --no-sync --project runtime/agentkit dataagent serve --env-file .env
```

If model configuration is provided through Home `.env` or process variables instead, omit `--env-file .env`. For A2A only, replace `serve` with `serve-a2a`. Backend-only installations can skip npm installation and frontend builds. `serve-a2a` has no REST health, session, or artifact endpoints. To connect frontends to an existing backend, see [Web/TUI connections](agentkit-package.md#connect-web-and-tui).

## Model gateway and validation

In the repository `.env`, set `LLM_BASE_URL` to a model gateway reachable from the deployment environment and configure its `LLM_MODEL` and `LLM_API_KEY`. Start from the repository `.env.example`. The model gateway and package mirrors serve separate purposes.

```bash
./deploy.sh doctor
./deploy.sh deploy --non-interactive
./deploy.sh status
```

`doctor` reports the effective npm registry, configured mirror responses, and local Python discovery. It uses HEAD requests with timeouts, installs nothing, and omits URL credentials and query parameters from output. An HTTP response proves only that the endpoint responds: a directory returning 403 / 404 may still serve individual artifacts. Actual deployment must verify authentication, artifact availability, and platform compatibility.

Installation diagnostics use `backend.log`; Web and TUI builds use `web.log` and `tui.log` under `<DATAAGENT_HOME>/runtime/logs/`. All stages reuse these three log files.

`npm run smoke:native-deploy` uses a temporary checkout to test installation, Python setup, Web/TUI builds, service lifecycle, and health checks, inheriting these environment variables. Without model configuration, `/healthz` should return 200 and `/ready` should return 503. Local tests verify the general workflow; use the actual mirrors and target machine to complete end-to-end validation in the deployment environment.

Python preparation, deployment preflight, and startup use the same environment variable precedence. uv manages interpreter selection and environment updates, and may recreate the project virtual environment when the Python selection changes. A custom environment path must refer to a virtual environment dedicated to this project. TUI startup also requires an existing environment and directs you to `npm run prepare:python` when it is missing.
