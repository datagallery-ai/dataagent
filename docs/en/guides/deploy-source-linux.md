# Linux one-click source deployment

Use `deploy.sh` on Linux to install dependencies, build Web and TUI, and run Web and Server in the background. If you need to manage dependencies or processes yourself, follow [manual source deployment](deploy-source-manual.md).

<a name="linux-prerequisites"></a>

## Prepare the environment

You need Git, uv, Node.js 22.12.0 or newer, and npm. The backend supports Python 3.11 and 3.12. If Node.js is missing or too old, the script can install it after confirmation through `apt-get`, `dnf`, or `yum`. On a system without those package managers, install Node.js first. For internal packages, indexes, or a preinstalled Python interpreter, set up [custom mirrors](deploy-mirrors.md) before deploying.

For WSL 2, check the [WSL 2 resource sizing](deploy-source.md#wsl2-sizing) before deploying.

<a name="linux-deploy"></a>

## Deploy and verify

```bash
git clone https://gitcode.com/datagallery/dataagent.git
cd dataagent
./deploy.sh
```

After deployment, Web and Server run in a detached process group. Closing the terminal or pressing Ctrl+C while following the logs leaves the services running. The script also builds the TUI. Start it separately with `./deploy.sh tui`.

Set `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY` in the repository root `.env`, then run `./deploy.sh restart`. Open the address printed by the script, usually [http://127.0.0.1:3000/data-tasks](http://127.0.0.1:3000/data-tasks). [Send your first question](../quick-start.md#run-your-first-question) and verify you receive a streamed reply.

<a name="managed-configuration"></a>

## Configuration rules

The first deployment creates the root `.env` and asks for listen ports and the client-reachable address. If Home has no `config.yaml`, the script creates one from the template. In an existing file, it updates only `server.host` and `server.port`. If the YAML is invalid, the script keeps the original file. You can complete deployment before supplying model credentials and set them before the first question.

On later interactive deployments, the script skips the prompts if a managed service is running or the root `.env` has content. To change the ports or client-reachable address, use `--reconfigure`. The script first backs up `.env` and keeps existing credentials.

```bash
./deploy.sh deploy --reconfigure
```

For unattended deployment, use `--non-interactive`. A port conflict or an installation that needs a sudo password fails immediately. The two flags are mutually exclusive and apply to `deploy`.

```bash
./deploy.sh deploy --non-interactive
```

<a name="lifecycle-commands"></a>

## Lifecycle commands

```bash
./deploy.sh status    # check processes and API / Web health
./deploy.sh start     # launch with existing dependencies and builds
./deploy.sh stop      # stop the managed process group
./deploy.sh restart   # validate configuration and restart
./deploy.sh logs      # follow runtime logs
./deploy.sh doctor    # check dependencies, configuration, ports, disk, and health
./deploy.sh tui       # run the TUI in this terminal when the API is ready
./deploy.sh help
```

`deploy` installs dependencies and builds. Redeployment stops the managed process group, so run it during a maintenance window. `start` and `restart` reuse the existing Python environment and build artifacts. If dependencies or artifacts are missing, run `deploy` again.

`deploy`, `restart`, and `doctor` use explicit `--config`, `--web-host`, and `--web-port` values first, then settings from the running managed deployment, and finally Home configuration and the default Web address. Interactive deployment prompts only for unspecified ports. When switching to an overlapping address on the same port, the script uses `ss` to identify the existing listener. If another process owns it or ownership is unclear, the current service keeps running and the command reports the conflict.

`restart` validates the configuration before stopping the running service. `doctor` performs the same validation if the Python environment exists. Otherwise it checks the listen settings and reports what it skipped. For mirror connectivity or deployment-directory permissions, inspect the `doctor` result and the deployment output.

<a name="managed-logs"></a>

## Follow deployment logs

`./deploy.sh logs` follows managed-service logs. See [Troubleshooting](troubleshooting.md) to find the right Server, Web, or TUI log and request IDs for a symptom. If you serve the workbench through a reverse proxy, see [browser error reporting](web.md#web-origin).
