# DataAgent

A local product built on **Deep Agents 0.7.12**, **LangChain 1.3.18** and the
official **AG-UI LangGraph adapter**. The frontend is the existing `apps/tui`;
runtime contains no UI copies or Python TUI forwarding layer. The legacy runtime
and authenticated TUI path remain unchanged.

`runtime/agentkit` is the independently buildable project directory. The Python SDK
is imported as `dataagent`; the distribution and CLI are also named `dataagent`.
Home/session paths and the TUI's `--v2` flag are unchanged. The versioned internal
protocol and graph identifiers are not installation or command names.

The [architecture and configuration guide](docs/architecture-and-configuration.md)
describes the current layers, every core module, package docstrings, configuration
precedence, workspace dependencies, and native Agent assembly.
The [plugin compiler and Python Hook design](docs/plugin-compiler-native-hooks.md)
defines the six native lifecycle points, function contracts and migration rules.
The [Runtime boundary design](docs/runtime-boundaries.md) explains configuration,
paths, capability references and provenance, including Python field migrations.

完整配置见 **[examples/config.json](examples/config.json)**；中文字段说明和文件清单见
**[examples/README.md](examples/README.md)**，包含默认值、六类 Hook、模型切换、插件目录、配置合并和环境变量优先级。
配置仅使用标准 JSON，不支持注释、尾逗号或重复键。
它带有两个可运行的 Python Hook 示例；仅需默认值且不启用独立 Hooks 时，使用
[config.example.json](config.example.json)。真实凭证仍放在本地 `.env`，不要提交到仓库。

## Quick start

Requirements: macOS/Linux, Python 3.12, uv, Node.js 22+ and installed repository
Node dependencies. From the repository root:

```bash
uv sync --project runtime/agentkit --locked
npm run start:tui -- --v2
```

First startup creates `~/.dataagent/config.json`, `.env`, `skills/`, `hooks/` and
`plugins/`, without overwriting existing files. If model settings are missing,
startup stops and tells you which variable and file to fill in. Edit
`~/.dataagent/.env` with `LLM_MODEL`, `LLM_BASE_URL` and `LLM_API_KEY`, then run again.
There is no registration, web server or configuration wizard.

Existing installations must convert Home/explicit configs to `config.json`, plugin manifests
to `.plugin.json`, and referenced SubAgent declarations to `.json`. Convert the content, not
only the extension; YAML files are no longer loaded. External files are not automatically migrated.
Skill `SKILL.md` frontmatter remains in Deep Agents' native format; `.env` is unchanged.

Product configs use `"schema_version": 3`
and plugin manifests to version 2. Startup never overwrites your existing configuration.
Replace old `hook_handlers` / `handler/events/match` declarations using the examples below.
Changing only the version number does not migrate old Hook code.
Hooks now accept only Python functions. Remove `command`, per-hook `timeout_seconds`
and error events; replace `handle(event, config)` and action responses with the native
function signatures below. Existing version-3 configs with `"hooks": []` keep their semantics after format conversion.

To use an existing repository-root `.env` without copying credentials:

```bash
npm run start:tui -- --v2 --env-file .env --workspace "$PWD"
```

Home (`DATAAGENT_HOME` or `~/.dataagent`) is the single working root. Without
explicit read-only inputs, `workspaces` is empty; startup creates no default input
directory. Outputs remain under Home's per-user/session runtime directory.
Explicit inputs (`--workspace`, `DATAAGENT_WORKSPACE`, or JSON `dataagent.workspaces`)
must already exist. Resuming a session keeps its saved input binding. Relative CLI
paths use the directory where npm was launched, not `apps/tui`. This cwd is only a
relative-path base: no configuration or extensions are discovered there.

The TUI builds through the existing npm entrypoint, starts its own Python backend,
checks its identity and opens the chat. Try `Summarize 1, 2, 3.` or explicitly ask
to delegate the statistics to `general-purpose` and read its Skill first.

Commands: `/help`, `/clear` (new conversation, no deletion), `/resume`,
`/resume <threadId>`, `/resume latest`, `/exit`. Add `--resume [threadId]` at startup.
Chat shows `Running…` while waiting for a validated terminal event, followed by a
separate `✓ Run completed · …s`, `✗ Run failed · …s`, or
`Interrupted — completion unknown · …s` summary. Time runs from request dispatch
through stream completion, excluding queue time. Failures preserve partial output
and the actual error code/message without replaying the request. Summaries are UI
records only: they do not enter checkpoints or reappear as fabricated timings on resume.

The input starts at one row, grows upward to six, and scrolls internally beyond that.
Its model footer stays on the bottom row at 60/80 columns. Chat uses a single column
at every width; `/outputs` opens the full output list. Enter submits; Shift+Enter (CSI-u or xterm
modifyOtherKeys), Alt+Enter and Ctrl+J insert a newline. V2 explicitly enables CSI-u
without startup probing. If iTerm2 sends CR for both Enter and Shift+Enter, configure
Shift+Enter to send ESC + `[13;2u`, or use Ctrl+J. Ctrl+Z/fg restores terminal state
and remeasures the real cursor. `NO_COLOR=1` is supported.

In V2, each `/outputs` reads the current session's `outputs/` directory, including nested
files created by native file tools or shell commands, also after `/resume` or a restart.
It does not require custom AG-UI artifact events. Internal conversation history and large
tool-result spill files are excluded; symbolic links are not followed. Enter previews
UTF-8 text (up to 64 KiB); other files show metadata and can be opened locally. Listing
failures are reported rather than shown as an empty list.

Automated PTY checks do not replace manual IME candidate-window verification.
See the [architecture and configuration guide](docs/architecture-and-configuration.md)
for current boundaries and executable verification entrypoints. The planned user and
multi-session filesystem isolation model is documented in
[docs/user-session-isolation.md](docs/user-session-isolation.md).

## Home, read-only inputs and runtime state

```text
~/.dataagent/                      # Override with process DATAAGENT_HOME
  config.json                     # Personal defaults
  .env                            # Credentials; not copied from another product
  skills/<name>/SKILL.md           # Independent personal Skills
  hooks/                          # Explicitly referenced Python functions
  plugins/<id>/.plugin.json        # Complete plugins; enable explicitly

~/.dataagent/runtime/             # Internal state; actual Home may be DATAAGENT_HOME
  checkpoints.sqlite               # Native LangGraph state/messages
  sessions.sqlite                  # User/session index
  backend.lock                     # Live OS lock, not a PID or stale-file test
  users/<user>/sessions/<thread>/
    outputs/                       # Agent write outputs and artifacts
    logs/                          # Reserved session log directory
    traces/<run_id>.json            # Standard OTLP JSON, saved after execution
```

New Home directories use 0700; generated config/env files use 0600. Existing files
and permissions are preserved. Installation, imports, `--help` and default SDK
preparation do not initialize Home.

Workspace directories are read-only Agent inputs by prompt convention, not enforced
permissions; internal state is under Home runtime.
Input directories do not supply configuration or extensions. Artifact files may be
referenced by checkpoints: they are not disposable cache. Existing session bindings
and files are not migrated or deleted.

## Configuration rules

JSON layers merge from low to high: built-in defaults → Home `config.json` →
explicit `--config` file. The explicit
file is an additional **partial layer**, not a replacement. Launch-directory
`config.json` is not searched. Each layer rejects unknown fields and duplicate JSON keys.

- Models merge by ID and field; scalars override only when declared.
- Independent `dataagent.hooks` accumulate in Home → explicit
  config order. `"hooks": []` adds nothing; it does not clear earlier registrations.
  Each Hook retains its declaring config directory. Repeated registrations remain distinct.
- Other lists, including `plugins.enabled` and `plugins.paths`, replace earlier
  values; an explicit empty list clears the lower layer for these fields.
- Paths keep their declaring file's directory as their base.
- Selecting the same real config explicitly skips its automatic layer and applies
  it only once as the explicit layer; extension paths still use its directory.
- Only the selected model's environment references are expanded.
- Restart after changing configuration or extensions.

Environment priority: process > `--env-file` > explicit `--config` sibling `.env` >
Home `.env`. Empty values do not erase lower nonempty values. Input-directory
`.env` files are **not** automatically read. The effective environment is local to
preparation and never mutates `os.environ`.

`DATAAGENT_HOME` and `DATAAGENT_V2_INSTANCE_ID` are process-only;
entries in env files are ignored with a warning. TUI variables such as
`DATAAGENT_V2_LOG_PATH`, `DATAFOUNDRY_TUI_THEME` and `INIT_CWD` also must be set in
the TUI process environment: backend `--env-file` does not configure the TUI.

## No project configuration layer

Only Home and explicit `--config` files configure the runtime. Startup never searches
`<cwd>/.dataagent` or input directories, and never asks for project trust. The old
`project_mode`, `--trust-project` and `--no-project` options have been removed.
See [the cleanup design](docs/home-root-cleanup.md) for exact boundaries and the
planned MCP locations: `<home>/.mcp.json` and enabled `<plugin_root>/.mcp.json`.
MCP loading is now available; see [MCP configuration and usage](docs/mcp.md).

## MCP tools

Use `.mcp.json` at Home or an enabled plugin root, with a `mcpServers` object.
Supported transports: `stdio` (default), `http` (Streamable HTTP), and `sse`.
Environment references use the existing `$env{NAME}` syntax and dotenv precedence.
Server tools receive namespaced native names, for example `mcp__user__math_add`.
Tools load before backend readiness; configuration/discovery failures stop startup.
No OAuth, resources/prompts, automatic retries or hot reload are added.

The official LangChain adapter opens/closes a session per tool invocation; stdio
servers restart per call. This does not support cross-call in-memory server state.

REST/TUI reuse the graph for each session. Installed/enabled extensions and local MCP
configuration changes take effect before the next query, without resetting conversation
history. Invalid updates reject that query; active runs keep their existing graph.
Model/server/path settings still require a restart. See [extension refresh](docs/extension-refresh.md)
for scope, bounded caching, and explicit SDK refresh.
SDK users call `tools = await load_mcp_tools(runtime)` before passing
`mcp_tools=tools` to `build_agent`. No MCP configuration means existing usage is unchanged.
See the [local example](examples/mcp/.mcp.json) and [complete contract](docs/mcp.md).

## Model request retries

`models.<id>.max_retries` defaults to `2` (up to three HTTP attempts); use `0` to disable.
Existing configurations that omit it gain the default after restarting. Merge this fragment
into your existing model entry; keep its name, URL and credentials:

```json
{"models": {"primary": {"max_retries": 2}}}
```

This uses the native SDK's backoff for connection/timeouts and retryable HTTP responses
(408, 409, 429 and 5xx, subject to provider retry headers). It does not retry the Agent,
Hooks, tools, or reads of an already-started response stream. Root and child Agents use
the same configured model. SDK-internal attempts do not count as separate Agent model loops.
REST's whole-run deadline includes retries and backoff; SDK hosts control their own overall
deadline. Retries can add latency and provider charges; they do not guarantee recovery.

Final stream failures log the run/thread IDs, `stage=agent_stream`, and a bounded, redacted
exception-chain message in Home's `runtime/logs/backend.log`. Exception types are retained
even when their messages are empty. No raw traceback, request body or debug logging is added.
Upstream error events are logged too, but cannot restore a cause already discarded upstream.

## Plugins, Skills and Hooks

Plugins are discovered in packaged builtins, Home and explicit `plugins.paths`. Only enabled IDs compile; duplicate enabled IDs at
different paths fail. IDs `user` and `cli` are reserved.
No plugin dependencies are installed automatically.

Independent Skills require only `skills/<name>/SKILL.md`; Home
Skills accumulate alongside enabled plugin Skills, even when a config adds no Hooks.
Native source labels are
plugin IDs and `User`. SubAgents with omitted `skills` inherit the root's compiled
sources; explicit lists restrict sources and `[]` loads none. Common's general-purpose
uses inheritance, including Home Skills. Real-file duplicates are
accepted, conflicting Skill names fail. Skill directory symlinks are allowed.

Define a personal Python Hook in Home config. The grouped syntax is normalized internally
to `HookSpec(event, entrypoint, matcher, params)`:

```json
{
  "schema_version": 3,
  "dataagent": {
    "hooks": {
      "before_agent": [],
      "after_agent": [],
      "before_model": [
        {
          "entrypoint": "hooks/check_input.py:handle",
          "params": {
            "max_chars": 4000
          }
        }
      ],
      "after_model": [],
      "before_tool": [
        {
          "entrypoint": "hooks/check_tool.py:handle",
          "matcher": "common__summarize_numbers",
          "params": {
            "blocked_tools": []
          }
        }
      ],
      "after_tool": []
    }
  }
}
```

`matcher` is optional and allowed only for tool events. It exactly matches the registered
tool name (no glob/regex); omitting it applies the hook to every tool. Entries must not
repeat `event` inside a group. Unknown events fail validation. Empty groups add nothing
and do not clear inherited hooks. Legacy flat lists remain supported. Plugins and
declarative subagents accept the same grouped syntax.

Each entrypoint exports a plain `def` or `async def`; no decorator is required:

| Events | Function signature | Return value |
| --- | --- | --- |
| `before_agent`, `after_agent`, `before_model`, `after_model` | `handle(state, runtime, *, params)` | `None`, native state-update dict or `Command` |
| `before_tool` | `handle(request, *, params)` | `None` |
| `after_tool` | `handle(request, result, *, params)` | `None` |

`params` is an explicit keyword-only parameter and receives a fresh copy per call.
Other arguments are native objects, not serialized payloads: read `state["messages"]`,
`runtime.context`, or `request.tool_call` directly. Return state updates instead of
mutating state in place; native reducers combine them. Tool hooks inspect the request
and result, without replacing them. Raise an exception to reject execution.

Four state hooks compile through LangChain's native decorators. Tool hooks use a thin
`wrap_tool_call`/`awrap_tool_call` adapter because upstream has no separate before/after
tool methods. Before hooks run in declaration order; after hooks run in reverse order.
`after_agent` is the native finalization node, not a graph-success callback or a `finally`.
`after_tool` runs on a normally returned result (including error ToolMessages), not
when the wrapped call raises. Hook failures propagate; none are swallowed or retried.
There are no command hooks, error events, action protocol or per-hook timeouts.
The existing REST whole-run deadline still applies. Async hooks require `ainvoke`/`astream`;
sync hooks support both sync and async calls. Cancelling async work cannot forcibly stop
a synchronous function already running in a thread.

Plugins and SubAgents may declare the same `hooks` entries. Root declarations affect
only the root Agent; children require explicit declarations. Host limits apply
independently to every declared Agent, including common's general-purpose. Plugin order is enabled order,
followed by the effective product extension list; duplicate declarations run twice.

Hooks are the only configured lifecycle extension. Remove `middleware` and `callbacks`
from product, plugin and SubAgent declarations, including empty lists: they now fail
validation. Rewrite supported behavior as plain Python hooks; arbitrary middleware and
callback behavior cannot be translated automatically into the six events.
The example common audit also uses these hooks, explicitly declared for the root and
general-purpose Agent. It logs event/Agent labels and tool-call identity, not payloads or a
complete execution tree. After hooks are not proof of final graph success.

Common now owns the general-purpose declaration and prompt; `agent.py` only adds host
policies. It also includes number-summary and tabular-inspection Skills, the numeric
summary tool, and an empty `.mcp.json` extension point. Disabling common removes these
declarations; Deep Agents may still provide its own default general-purpose without
our declared-child limits or common audit Hooks. See [common](examples/plugins/common/README.md).

Common is an external example/test plugin, not bundled or enabled by default. To try it,
copy `examples/plugins/common` (including `.plugin.json` and `.mcp.json`) to
`<home>/plugins/common`, then add `"common"` to `plugins.enabled` in `<home>/config.json`.
Home plugins are discovered automatically; no `plugins.paths` entry is needed.

See [examples/config.json](examples/config.json), [Python input check](examples/hooks/check_input.py)
and [Python tool check](examples/hooks/check_tool.py). From the repository root,
after migrating existing Home configuration:

```bash
npm run start:tui -- --v2 --config runtime/agentkit/examples/config.json --workspace "$PWD"
```

## SDK and backend boundary

Keep a dedicated V2 environment; the legacy runtime shares the `dataagent` import name.

```python
from dataagent import LaunchOptions, build_agent, prepare_runtime

runtime = prepare_runtime(LaunchOptions(workspace="/existing/data"))
graph = build_agent(runtime, checkpointer=checkpointer)
result = await graph.ainvoke({"messages": [("user", "Summarize 1, 2, 3")]}, config)
```

The caller supplies/owns the checkpointer and run config. SDK preparation defaults
to `init_home=False`; a missing Home is simply an absent user layer. Request templates
explicitly with `init_home=True`. SDK never reads a terminal.

`dataagent.prepare_runtime` (implemented in the `dataagent.bootstrap` package) owns paths,
initialization, environment, configuration and resource discovery. It
returns an immutable Runtime. Package layout and dependency rules:
[architecture and configuration guide](docs/architecture-and-configuration.md).
`dataagent.declarations` defines the shared Tool/Hook/Plugin/SubAgent schemas;
`dataagent.settings` defines product settings. `dataagent.extensions.compile_extensions`
combines selected plugins with standalone `skill_sources` and `hook_entries`, returning
native Deep Agents kwargs. No HostBindings object or virtual plugin is required.
`build_agent` returns the native `CompiledStateGraph`. It does not launch a service.
For SDK-only tracing, use the graph's native API:

```python
graph = build_agent(runtime).with_config(callbacks=[your_native_callback])
```

The previous `build_agent(..., callbacks=...)` argument is removed. Callbacks are
native run configuration, not plugin configuration or a second configured Hook path.
`restapi/__main__.py` is the only backend launch implementation; both
`dataagent serve` and `python -m restapi serve` call it. REST consumes Runtime,
not configuration files or discovery rules.

Manual backend command:

```bash
uv run --project runtime/agentkit dataagent serve --workspace /existing/data
```

Default address: `127.0.0.1:8790`. Port conflicts fail without touching other
services. One live backend owns each state directory.

| Route | Purpose |
|---|---|
| `GET /healthz` | Instance identity, model, plugin IDs, Home, read-only inputs, stateDir, deadline |
| `POST /dataagent/stream` | One new user message, native AG-UI SSE |
| `GET /sessions?limit=50` | Recent sessions, at most 100 |
| `GET /sessions/{threadId}` | Last successful checkpoint and recovery notice |
| `GET /sessions/{threadId}/outputs` | Current session's output files, newest first |
| `GET /sessions/{threadId}/outputs/preview?path=<relative-path>` | UTF-8 text preview, up to 64 KiB |

The V2 service listens on loopback with no login or Bearer token verification.
Health exposes Home, inputs and stateDir, never configuration
contents or keys. TUI logs use stateDir; missing stateDir is an error. External mode
`--v2 --runtime-url http://127.0.0.1:8790/dataagent/stream` never stops that service
and cannot combine with local config/env/workspace arguments.

## Local execution traces

`build_agent()` binds `LocalLangChainTracer` from `dataagent/extensions/tracing.py`.
It reuses native LangChain callbacks and the LangSmith SDK's OpenTelemetry conversion:

```text
LangChainTracer → Client(tracing_mode="otel") → TracerProvider
               → SimpleSpanProcessor → LocalFileExporter → OTLP JSON
```

No LangSmith account, API key, collector, or tracing service is required. The dedicated
provider has only a local file exporter; remote tracing environment variables do not
change its destination. An explicit `tracing_context(enabled=False)` disables tracing.
Separately supplied remote tracers/clients remain the caller's responsibility.

SDK calls and TUI/REST turns save one **standard OTLP JSON object** when the root run
completes, fails, or is gracefully cancelled:

```text
<home>/runtime/users/<user_id>/sessions/<thread_id>/traces/<run_id>.json
```

The file uses `resourceSpans → scopeSpans → spans`, hexadecimal trace/span IDs,
Unix nanosecond timestamps, numeric enums, parent span links, attributes and errors.
It can be imported into viewers that accept OTLP JSON. It is not the former custom
`{run_id, agents, messages}` trajectory format; old files are not converted automatically.

Spans include the Agent, subagents, LLM calls, tools, and middleware timings. Agent,
LLM and tool input/output attributes may contain system prompts, conversation history,
and full tool results. Internal middleware payloads are omitted because they can
contain live runtime/model objects. Token events are not retained. An unsuccessful
tool result is not necessarily a raised exception: inspect its output/exit code too.

The filename and trace ID use the native root run UUID (hex without hyphens for OTLP).
Native run/parent identities are retained as `langsmith.metadata.dataagent_run_id` and
`langsmith.metadata.dataagent_parent_run_id`. HTTP request IDs are recorded under
`langsmith.metadata.dataagent_root_run_id`; thread IDs under `langsmith.metadata.thread_id`.
The SDK's generated span IDs are retained, while parent links are reconciled with the
native run hierarchy. If `enabled="local"` omits a middleware span, its descendants link
to their closest exported ancestor. Compatibility handling for pinned LangSmith 0.12.5
also preserves final error statuses and releases SDK entries retained after batching.

The root completion callback drains the SDK queue, flushes the provider, then commits
only that trace's spans. Concurrent invocations share the exporter but not their files.
Completed trace buffers and native run maps are released. This is completion-time
export, not a live event log: a forcibly killed process may leave no file, and unfinished
calls without end callbacks are not fabricated. Traces use mode 0600 and redact known
runtime credentials and common API-key/Bearer patterns, but still contain business
data. Large runs can consume substantial memory/disk; there is no automatic retention
policy. Saving failures log `TRACE_SAVE_ERROR` without changing the Agent result.

REST does not manage tracing. SDK callers need no extra setup; without an explicit
`session`, `build_agent()` uses the `sdk` session directory. Additional native callbacks
can still be supplied through invocation `config["callbacks"]`.

## Security boundary

File tools and shell use native `LocalShellBackend` with real host paths
(`virtual_mode=False`) and the current session's `outputs/` as their default working
directory. `CompositeBackend` stores artifacts under `outputs/artifacts/`; there are
no `/output` or `/workspace/<n>` virtual mounts.

**There is no tool access sandbox or custom file-path whitelist.** Both file tools
and shell commands can read or modify any path allowed by the backend process's host
permissions, including configuration files, business inputs and other sessions.
Treating workspaces as read-only and writing generated files to outputs are prompt
conventions, not enforced restrictions. Session directories organize resources;
they do not isolate tool access. Shell inherits the backend process environment;
timeouts, output truncation and exit codes use native behavior.

Use this mode only for trusted local work, not untrusted or multi-user services.
REST output path validation, session ownership checks and private Home directory
permissions remain in place; they do not constrain the Agent's host-level tool access.
Logs/errors redact credentials; Python extensions and hooks are not sandboxed.

## Verification

```bash
uv run --project runtime/agentkit pytest runtime/agentkit/tests -q -m 'not live'
uv run --project runtime/agentkit ruff check runtime/agentkit
npm test --workspace apps/tui
```

Tests isolate Home/workspace and use synthetic data. The full deterministic product
test runs real TUI/backend/tool/Skill/SubAgent/checkpoint/restart paths, with only the
remote provider scripted. For an explicitly authorized real-provider run:

```bash
DATAAGENT_V2_LIVE_ENV=/absolute/path/to/.env \
  uv run --project runtime/agentkit pytest runtime/agentkit/tests/test_product_e2e.py -m live -q
```

GUI, full legacy capability migration, SQL/NL2SQL, MCP/A2A, shell, approvals,
multiuser service and dynamic model editing remain outside this release.
