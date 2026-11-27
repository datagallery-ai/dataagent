<a name="deploy-from-source"></a>

# Deploy from source

Get the source from the [DataAgent repository on GitCode](https://gitcode.com/datagallery/dataagent). Choose a path for your environment.

| Environment | Guide |
| --- | --- |
| Linux, with scripted dependency installation, builds, and managed Web and Server processes | [Linux one-click deployment](deploy-source-linux.md) |
| Windows, macOS, or Linux with independently managed dependencies and processes | [Manual source deployment](deploy-source-manual.md) |
| An existing Server wheel or TUI npm package, or an artifact-based deployment | [Package deployment](deploy-packages.md) |
| Restricted downloads that require internal mirrors or a preinstalled Python interpreter | [Custom mirrors](deploy-mirrors.md) |

<a name="requirements"></a>

<a name="source-requirements"></a>

## Before you deploy

Prepare Git, Node.js 22.12.0 or newer, npm, uv, and an OpenAI-compatible model service that the Server can reach. The backend supports Python 3.11 and 3.12. On Linux, the one-click script can install missing Node.js through a system package manager after confirmation. Follow the prerequisites in the guide for your chosen path.

DataAgent is deployed from source or packages. The repository currently ships no Docker or Compose deployment definition for DataAgent itself. The control plane has no registration or login. Read the [security boundaries](../concepts/security.md) before configuring access.

<a name="wsl2-sizing"></a>

### WSL 2 resource sizing

Use these allocations to install and build from source, then run the Server, Web, and one TUI on WSL 2. CPU and memory refer to the WSL 2 allocation. Disk refers to free space before installation.

| Use case | Logical CPUs | Memory | Free disk |
| --- | ---: | ---: | ---: |
| Suggested minimum for one user | 4 | 6 GiB | 15 GiB |
| Daily development and repeated builds | 8 | 10–12 GiB | 25 GiB |

Keep the checkout and dependencies in the WSL Linux filesystem, for example under `~/dataagent`, to reduce cross-filesystem I/O during builds. The disk budget includes dependency caches and build headroom. Budget separately for datasets, long-term logs, and session history. These allocations assume a remote model provider and light data workloads. Size local model hosting, Spark, and large data workloads separately.

On Windows, set `processors`, `memory`, and `swap` in `%UserProfile%\.wslconfig`. Leave memory available for Windows and other applications when setting the limit. See [WSL filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems) and [WSL 2 settings](https://learn.microsoft.com/en-us/windows/wsl/wsl-config).

<a name="previous-sections"></a>

## Where to find the previous sections

| Previous section | Current documentation |
| --- | --- |
| <a name="recommended-linux-one-click-deploy"></a>Linux one-click deployment | [Deploy and verify](deploy-source-linux.md#linux-deploy) |
| <a name="configuration-rules"></a>Configuration rules | [Ports and configuration](deploy-source-linux.md#managed-configuration) |
| <a name="lifecycle-commands"></a>Lifecycle commands | [Lifecycle commands](deploy-source-linux.md#lifecycle-commands) |
| <a name="log-files"></a>Log files | [Follow deployment logs](deploy-source-linux.md#managed-logs) |
| <a name="windows-macos-other-manual-npm-deploy"></a><a name="windows--macos--other-manual-npm-deploy"></a>Windows / macOS / other manual deployment | [Manual source deployment](deploy-source-manual.md) |
| <a name="1-prepare-tools-and-download-sources"></a><a name="manual-prerequisites"></a>Prepare tools and download sources | [Tools and download sources](deploy-source-manual.md#manual-prerequisites) |
| <a name="2-install-dependencies"></a>Install dependencies | [Install dependencies](deploy-source-manual.md#manual-dependencies) |
| <a name="3-configure-the-model-service"></a>Configure the model service | [Model configuration](deploy-source-manual.md#manual-model) |
| <a name="4-build-and-start-same-for-formal-test-and-real-production"></a><a name="4-build-and-start-same-for-formal-test-and-real-production-"></a>Build and start | [Build and start](deploy-source-manual.md#manual-start) |
| <a name="5-verify-health-and-model-calls"></a><a name="manual-verification"></a>Verify health and model calls | [Health and model checks](deploy-source-manual.md#manual-verification) |
| <a name="connect-source-frontends"></a><a name="connect-source-frontends-to-an-existing-server"></a>Connect source frontends to a Server | [Connect Web](web.md#connect-server) · [Connect TUI](tui.md#connect-server) |
| <a name="use-a-compiled-plugin-entry-from-source"></a>Use a compiled plugin entry | [Compiled subagents and entry mode](integrate/extensions/subagents.md#entry-mode) |
| <a name="start-the-tui"></a>Start the TUI | [TUI guide](tui.md) |
| <a name="troubleshooting"></a><a name="deployment-troubleshooting"></a>Troubleshoot deployment | [Troubleshooting](troubleshooting.md) |
| <a name="wrong-node-version"></a>Wrong Node version | [Node.js requirements](deploy-source-manual.md#manual-prerequisites) |
| <a name="page-does-not-load"></a>Page does not load | [Web troubleshooting](web.md#troubleshooting) |
| <a name="backend-not-running"></a>Backend not running | [Startup diagnostics](troubleshooting.md#startup) |
| <a name="model-unavailable"></a>Model unavailable | [Model diagnostics](troubleshooting.md#models) |
| <a name="port-conflict"></a>Port conflict | [Port and startup diagnostics](troubleshooting.md#startup) |
| <a name="the-model-cannot-read-a-local-directory"></a>Model cannot read local files | [Read-only workspaces](inputs.md#read-only-workspaces) |
| <a name="next-steps"></a>Next steps | [Send your first question](../quick-start.md) |
