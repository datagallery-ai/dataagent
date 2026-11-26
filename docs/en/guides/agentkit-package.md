# Python package deployment

DataAgent ships as the Python distribution `datagallery-dataagent`. Its import and CLI name are both `dataagent`. The wheel supports Python 3.11 and 3.12 and includes the API, A2A, SDK, Home templates, and built-in plugins. Web and TUI connect over HTTP and require Node.js ≥22.12.0. TUI is available as a separate npm package, and Web builds from source.

## Build

Run `make build` from the repository root to generate the wheel in `dist/server/` and the TUI npm package in `dist/tui/`. To build the Server on its own:

```bash
uv build --wheel --clear --out-dir dist/server
```

The wheel version comes from `pyproject.toml`. Installation resolves the dependencies declared in its metadata from the configured uv or pip index. For offline installation, prepare dependency artifacts for the target OS, architecture, Python version and selected extras.

## Install and start the backend

Copy the wheel into a deployment directory. Project sources are not required:

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python datagallery_dataagent-0.2.0-py3-none-any.whl
```

An environment with pip installed can also use `python -m pip install datagallery_dataagent-0.2.0-py3-none-any.whl`. The `uv pip` command above does not require pip in the target environment. On Windows, the interpreter and CLI are in `.venv\Scripts\`.

Variables such as `UV_PYTHON` select an interpreter for environment preparation tools. The installed `dataagent` command runs with its environment's Python and does not switch interpreters based on these variables. The package requires Python 3.11 or 3.12.

Configure a compatible model service reachable from the backend machine, then start the installed CLI:

```bash
export LLM_MODEL=your-model-name
export LLM_BASE_URL=https://your-openai-compatible-host/v1
export LLM_API_KEY=replace-with-your-key
.venv/bin/dataagent serve
```

First startup creates missing templates in Home (`~/.dataagent`, or process variable `DATAAGENT_HOME`). Package mode reads process variables, Home `.env`, or an explicit `--env-file`; it does not automatically read a source checkout's root `.env`. Listening uses `config.yaml` `server.host` / `server.port`, defaulting to `127.0.0.1:8790`. The installed `dataagent serve-a2a` command starts A2A separately.

## Optional dependencies

The default installation includes service startup, model conversations, SQL parsing, and SQLite, MySQL, and PostgreSQL drivers. MySQL uses `PyMySQL[rsa]` with SHA-2 authentication support; PostgreSQL uses `psycopg2-binary`, imported as `psycopg2`, with execution engine `gaussvector` and SQL dialect `postgres`; see the [nl2sql GV configuration](https://gitcode.com/datagallery/dataagent/blob/deepagents_refactor_new/dataagent/plugins/nl2sql/README.md#gaussvector-配置). These base data dependencies are installed by regular installation and `uv sync` without selecting an extra.

`psycopg2-binary` reduces deployment build prerequisites but bundles libraries such as libpq and OpenSSL. Maintain these through driver package updates; system library upgrades alone do not update them. `PyMySQL[rsa]` also installs cryptography for SHA-2 authentication.

Select these optional capabilities in the backend's Python environment:

| Capability | Extra | Dependencies |
| --- | --- | --- |
| Spark/Hive | `spark` | PySpark 4.0.x |
| Video reading and frame extraction | `video` | PyAV 16.x, Pillow 12.x |
| All optional capabilities above | `all` | References Spark and video extras; excludes development tools |

For example, install the local wheel with optional video support:

```bash
uv pip install --python .venv/bin/python \
  "./datagallery_dataagent-0.2.0-py3-none-any.whl[video]"
```

To install everything, use `"./datagallery_dataagent-0.2.0-py3-none-any.whl[all]"`. For source execution, prepare with `uv sync --no-dev --extra video`, then use `uv run --no-sync dataagent serve`. Omit `--no-dev` to include the development group, and use `--extra all` for every capability. Select the required extras on every synchronization, since `uv sync` removes unselected packages.

Video errors in core retain the upstream wording. If a message suggests `deepagents[video]`, install `datagallery-dataagent[video]` for this product; a separate Deep Agents installation is unnecessary.

An extra installs Python dependencies; plugins and database settings still require configuration. Spark/Hive also require [Java 17 or later for Spark 4.0](https://spark.apache.org/docs/4.0.2/api/python/getting_started/install.html), `JAVA_HOME`, and the relevant services.

## Deploy the source service

```bash
./deploy.sh deploy
```

One-click deployment explicitly selects `--no-dev --extra video`, installing the base MySQL/PostgreSQL drivers and video dependencies without Spark/Hive or development tools such as pytest and ruff. It installs frontend dependencies once with npm and Python dependencies once with uv. Starting or restarting services does not install dependencies again.

For staged installation, use `npm install` for the frontends and `uv sync --no-dev --extra video` for the backend. Select Spark separately as described below. The wheel installs base runtime dependencies by default and provides `[all]` for every extra.

For Spark/Hive, prepare Java 17 or later and the relevant service configuration, then explicitly install `all`, which includes Spark:

```bash
./deploy.sh stop
uv sync --no-dev --extra all
./deploy.sh start
```

For a standalone package, select `[spark]` or `[all]`. The locked PySpark source archive is about 434 MB; a cold installation downloads and builds it, while a usable uv cache allows reuse. During initial source resolution or an index change, uv may download this archive for optional-dependency metadata even if Spark is not selected; retaining a resolution snapshot reduces these resolutions. A later default `deploy` removes Spark according to the default selection; repeat explicit preparation when needed. Installing dependencies does not configure databases or enable plugins.

## Connect Web and TUI

Copy the TUI tarball to the client machine, install it, and connect to the running Server:

```bash
npm install -g ./datagallery-tui-0.2.0.tgz
dataagent-cli --runtime-url http://127.0.0.1:8790
# Short alias
da --runtime-url http://127.0.0.1:8790
```

The installed TUI connects to a running Server. Start it with the Python package’s `dataagent serve` command and pass server startup options such as `--workspace` there. `dataagent-cli` and `da` start the same terminal client.

Use the following steps to run Web or TUI from source.

Use frontend sources from the package's commit or a verified compatible version. Prepare frontend dependencies and builds while skipping source Python preparation:

```bash
npm install --include=dev
npm run build
```

In another terminal, start Web with the existing `API_PROXY_TARGET` pointing to the package service:

```bash
API_PROXY_TARGET=http://127.0.0.1:8790 npm run start:web -- --web-host 127.0.0.1 --web-port 3000
```

Attach the TUI:

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8790
```

In PowerShell, set process variables using `$env:NAME="value"`. On separate hosts, replace the loopback address with a reachable backend address and configure its listen address.

These frontend commands attach to the installed service. To start the server from a source checkout, use `uv run --no-sync dataagent serve`. The `serve` command provides REST and A2A for Web and TUI connections. The `serve-a2a` command provides only A2A.

## Makefile pipeline

Use these commands to build and verify the pipeline artifacts:

```bash
make prepare UV_PYTHON=3.12
make build UV_PYTHON=3.12
make ut UV_PYTHON=3.12
make st UV_PYTHON=3.12
```

Installation and tests run `uv sync` directly against the configured index, without manual `uv.lock` rewrites. The repository retains a resolution snapshot without forcing frozen versions.

After `make build`, archive the wheel in `dist/server/` and the npm tarball in `dist/tui/`. The Web build stays in `ui/web/.next/`. The default `make ut` includes deployment and YAML checks. The `ut/st` targets prepare development and test tools automatically. Select the required extras during deployment.

## Verify and diagnose

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
curl -I http://127.0.0.1:3000/data-tasks
```

`/healthz=200` means the process is live; `/ready=200` means conversation engine configuration loaded. Send a question from Web/TUI to verify an actual streamed model response.

Missing model configuration returns 503 from `/ready`; a failed model call produces `RUN_ERROR`; a stopped backend produces Web proxy HTTP 502 and a TUI connection error. Processes retain `backend.log`, `web.log`, and `tui.log` under their respective Home directories. Builtin plugins are packaged; configure databases, models, and other external services separately.

MySQL, PostgreSQL, and SQL parsing dependencies are base runtime dependencies; repair the runtime installation if they are missing. Spark/Hive and video dependencies are installed through their respective extras.

After `make build`, run `npm run smoke:agentkit-package` from the development checkout to verify isolated installation and these success/failure paths with a local compatible model service. It exercises the real TUI connection and streaming client; terminal rendering and real model services require their own verification.
