# AgentKit package deployment

AgentKit retains the Python distribution name `datagallery-dataagent`, import name `dataagent`, and CLI `dataagent`. It builds into a wheel and source distribution containing the API, A2A, SDK, Home templates, prompts, and builtin plugins. The backend requires Python 3.12. Web/TUI require Node.js ≥22.12.0 and connect over HTTP; they are still built from frontend sources and are not part of the Python package.

## Build

With Node.js and uv available on the build machine, run from the repository root:

```bash
npm run package:agentkit
```

`make package` calls the same command. Packaging does not build Web/TUI or require frontend dependencies to be installed; `npm run build` / `make build` compile the frontends.

To build without the npm / Make wrapper, run the same native uv commands. This path requires uv and Python 3.12, without Node.js:

```bash
uv build --project runtime/agentkit --out-dir dist/agentkit
uv export --project runtime/agentkit --no-dev --all-extras \
  --no-emit-project --no-hashes --output-file dist/agentkit/constraints.txt
```

The default output directory is `dist/agentkit/`, containing `datagallery_dataagent-0.1.0-py3-none-any.whl`, `datagallery_dataagent-0.1.0.tar.gz`, and `constraints.txt`. The version comes from `runtime/agentkit/pyproject.toml`. The builder creates the source distribution first, then builds the wheel from it. Exported constraints pin runtime dependencies and all extras, excluding development dependencies and dependency files. Constraints do not install optional packages. `constraints.txt` pins versions without hashes; it is a version snapshot for this build, not a hashed integrity manifest. Dependencies are downloaded from the configured index during installation. Reuse uv/pip mirror configuration.

Set `UV_DEFAULT_INDEX` for uv or `--index-url` for pip to select the installation index. Constraints do not carry source lockfile artifact URLs, so package installation does not require rewriting the lockfile or running a `sed` command.

The wheel and constraints do not include third-party dependency artifacts. Installation still requires a reachable package index. Fully offline installation requires dependency artifacts prepared separately for the target OS, architecture, Python version, and selected extras.

## Install and start the backend

Copy the wheel and constraints into a deployment directory. Project sources are not required:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -c constraints.txt datagallery_dataagent-0.1.0-py3-none-any.whl
```

An environment with pip installed can also use `python -m pip install -c constraints.txt datagallery_dataagent-0.1.0-py3-none-any.whl`. The `uv pip` command above does not require pip in the target environment. On Windows, the interpreter and CLI are in `.venv\Scripts\`.

Variables such as `UV_PYTHON` select an interpreter for environment preparation tools. The installed `dataagent` command runs with its environment's Python and does not switch interpreters based on these variables. The package still requires Python 3.12.x.

Configure a compatible model service reachable from the backend machine, then start the installed CLI:

```bash
export LLM_MODEL=your-model-name
export LLM_BASE_URL=https://your-openai-compatible-host/v1
export LLM_API_KEY=replace-with-your-key
.venv/bin/dataagent serve
```

First startup creates missing templates in Home (`~/.dataagent`, or process variable `DATAAGENT_HOME`). Package mode reads process variables, Home `.env`, or an explicit `--env-file`; it does not automatically read a source checkout's root `.env`. Listening uses `config.yaml` `server.host` / `server.port`, defaulting to `127.0.0.1:8790`. The installed `dataagent serve-a2a` command starts A2A separately.

## Optional dependencies

The default installation includes service startup, model conversations, SQL parsing, and SQLite, MySQL, and PostgreSQL drivers. MySQL uses `PyMySQL[rsa]` with SHA-2 authentication support; PostgreSQL uses `psycopg2-binary`, imported as `psycopg2`, with execution engine `gaussvector` and SQL dialect `postgres`; see the [nl2sql GV configuration](https://gitcode.com/datagallery/dataagent/blob/deepagents_refactor_new/runtime/agentkit/builtin-plugins/nl2sql/README.md#gaussvector-配置). These base data dependencies are installed by regular installation and `uv sync` without selecting an extra.

`psycopg2-binary` reduces deployment build prerequisites but bundles libraries such as libpq and OpenSSL. Maintain these through driver package updates; system library upgrades alone do not update them. `PyMySQL[rsa]` also installs cryptography for SHA-2 authentication.

Select these optional capabilities in the backend's Python environment:

| Capability | Extra | Dependencies |
| --- | --- | --- |
| Spark/Hive | `spark` | PySpark 4.0.x |
| Video reading and frame extraction | `video` | PyAV 16.x, Pillow 12.x |
| All optional capabilities above | `all` | References Spark and video extras; excludes development tools |

For example, install the local wheel with optional video support:

```bash
uv pip install --python .venv/bin/python -c constraints.txt \
  "./datagallery_dataagent-0.1.0-py3-none-any.whl[video]"
```

To install everything, use `"./datagallery_dataagent-0.1.0-py3-none-any.whl[all]"`. For source execution, prepare with `npm run prepare:python -- --no-dev --extra video`, then use `npm run start:api`; omit `--no-dev` to include the development group, and add `--extra all` for every capability. Synchronization follows the exact semantics of `uv sync`: select required extras on every synchronization; unselected packages are removed. Add `--no-sync` when launching directly with uv to avoid resolving the source lockfile again.

Video errors in core retain the upstream wording. If a message suggests `deepagents[video]`, install `datagallery-dataagent[video]` for this product; a separate Deep Agents installation is unnecessary.

An extra installs Python dependencies; plugins and database settings still require configuration. Spark/Hive also require [Java 17 or later for Spark 4.0](https://spark.apache.org/docs/4.0.2/api/python/getting_started/install.html), `JAVA_HOME`, and the relevant services.

## Deploy the source service

```bash
./deploy.sh deploy
```

One-click deployment explicitly selects `--no-dev --extra video`, installing the base MySQL/PostgreSQL drivers and video dependencies without Spark/Hive or development tools such as pytest and ruff. The npm installation skips its Python hook, then synchronizes Python once through the shared preparation entrypoint. Starting or restarting services does not install dependencies again.

The regular `npm install` / `npm ci` hook still selects `--no-dev` without extras. For staged installation, run `npm run prepare:python -- --no-dev --extra video` before starting services; explicitly prepare Spark as described below when needed. Direct `uv sync` retains its native defaults and does not automatically select `all`. The distribution also keeps its base runtime dependencies by default; use the wheel's `[all]` extra for every capability.

For Spark/Hive, prepare Java 17 or later and the relevant service configuration, then explicitly install `all`, which includes Spark:

```bash
./deploy.sh stop
npm run prepare:python -- --no-dev --extra all
./deploy.sh start
```

For a standalone package, select `[spark]` or `[all]`. The locked PySpark source archive is about 434 MB; a cold installation downloads and builds it, while a usable uv cache allows reuse. During initial source resolution or an index change, uv may download this archive for optional-dependency metadata even if Spark is not selected; retaining a resolution snapshot reduces these resolutions. A later default `deploy` removes Spark according to the default selection; repeat explicit preparation when needed. Installing dependencies does not configure databases or enable plugins.

## Connect Web and TUI

Use frontend sources from the package's commit or a verified compatible version. Prepare frontend dependencies and builds while skipping source Python preparation:

```bash
SKIP_POSTINSTALL_BUILD=1 npm ci
npm run build
```

In another terminal, start Web with the existing `API_PROXY_TARGET` pointing to the package service:

```bash
API_PROXY_TARGET=http://127.0.0.1:8790 npm --workspace @datagallery/web run start -- --hostname 127.0.0.1 --port 3000
```

Attach the TUI:

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8790
```

In PowerShell, set process variables using `$env:NAME="value"`. On separate hosts, replace the loopback address with a reachable backend address and configure its listen address.

These frontend commands attach to the package service. `npm run start`, `npm run start:api`, and `./deploy.sh` remain source deployment entrypoints and manage the checkout's Python environment. `serve` provides REST and A2A; `serve-a2a` provides A2A only, so Web/TUI must connect to `serve`.

## Makefile pipeline

Existing pipelines can keep these commands:

```bash
make prepare PYTHON_VERSION=3.12
make build PYTHON_VERSION=3.12
make package PYTHON_VERSION=3.12
make ut PYTHON_VERSION=3.12
make st PYTHON_VERSION=3.12
```

`PYTHON_VERSION`, `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`, and `NPM_REGISTRY` remain compatible Make arguments; native npm/uv settings take precedence. Installation and tests use native `uv sync` through the shared preparation entrypoint and the configured index, without manual `uv.lock` rewrites. The repository retains a resolution snapshot without forcing frozen versions.

`build` compiles Web/TUI; `package` produces Python distributions without compiling the frontends again. To archive Python artifacts, collect the wheel, source distribution, and `constraints.txt` from `dist/agentkit/`; frontend artifact directories remain the same. `ut/st` prepare development/test tools automatically, requiring no additional pipeline install command. Select deployment extras during deployment.

## Verify and diagnose

```bash
curl http://127.0.0.1:8790/healthz
curl http://127.0.0.1:8790/ready
curl -I http://127.0.0.1:3000/data-tasks
```

`/healthz=200` means the process is live; `/ready=200` means conversation engine configuration loaded. Send a question from Web/TUI to verify an actual streamed model response.

Missing model configuration returns 503 from `/ready`; a failed model call produces `RUN_ERROR`; a stopped backend produces Web proxy HTTP 502 and a TUI connection error. Processes retain `backend.log`, `web.log`, and `tui.log` under their respective Home directories. Builtin plugins are packaged; configure databases, models, and other external services separately.

MySQL, PostgreSQL, and SQL parsing dependencies are base runtime dependencies; repair the runtime installation if they are missing. Spark/Hive and video dependencies are installed through their respective extras.

Run `npm run smoke:agentkit-package` from the development checkout to verify isolated installation and these success/failure paths with a local compatible model service. It exercises the real TUI connection and streaming client; terminal rendering and real model services require their own verification.
