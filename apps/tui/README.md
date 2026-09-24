# DataFoundry TUI (`@datafoundry/tui`)

Terminal interface for the datafoundry monorepo.

- **User guide:** [TUI 指南](../../docs/zh/guides/tui.md)
- **Quick start:** [快速开始](../../docs/zh/quick-start.md)

From the repository root:

```bash
npm install
npm run start:tui
npm run start:tui -- --runtime-url http://127.0.0.1:8787/api/copilotkit
npm run start:tui -- --no-auto-login
```

## DataAgent V2 local mode

The existing TUI directly starts and manages `runtime/agentkit`'s backend.
No registration, web service, runtime UI directory or Python TUI forwarding is needed.

Using the repository-root `.env` with your model configuration:

```bash
uv sync --project runtime/agentkit
npm run start:tui -- --v2 --env-file .env --workspace "$PWD"
```

Home defaults live in `~/.dataagent`; the first local launch creates non-overwriting
templates. Fill in its `.env`, then `npm run start:tui -- --v2` works without a config flag.
Optional `--config /absolute/path/config.json` adds a partial layer above Home;
its sibling `.env` is also loaded by the backend. Home is the single working root,
selected with `DATAAGENT_HOME` (default `~/.dataagent`). Optional `--workspace` paths
are existing read-only inputs; without them the input list is empty. Relative paths
resolve from the npm launch directory, which supplies no configuration or extensions.
Add `--resume` or `--resume <threadId>` to restore
a session. `/exit` closes the UI and its owned backend. An occupied port fails without
attaching to or stopping another service.

For an already running V2 backend, use `--v2 --runtime-url http://127.0.0.1:8790/dataagent/stream`
without a login or token. That mode does not own or stop the service;
do not combine it with `--config`, `--env-file` or `--workspace`.

There is no project configuration layer or trust prompt. Logs/checkpoints live in
`<home>/runtime`; session outputs live in `runtime/users/<user>/sessions/<session>/outputs`.
Existing session input bindings are retained; no files are migrated or deleted. The backend
owns `--env-file` parsing, so TUI theme/log variables must be in the TUI process environment.

See the [V2 configuration and plugin guide](../../runtime/agentkit/README.md).

## Chat and keyboard behavior

The composer starts with one input row and grows upward to six visible rows
(fewer on short terminals), then scrolls internally. Its borderless gray surface
and model/workspace footer use the full chat width. The normal empty control area
is four rows. Both home and chat support
60-column terminals; the minimum height is 19 rows.

- Enter submits. Shift+Enter (CSI-u or xterm modifyOtherKeys), Alt+Enter and Ctrl+J insert a newline.
- Multi-line bracketed paste stays one editable draft; large pastes keep the existing fold/expand behavior.
- Up/Down navigate the draft and command history. Messages entered during a run remain queued.
- `/` opens commands and Tab completes. V2 exposes `/help`, `/clear`, `/resume [id]`, `/outputs` and `/exit`.
- `/outputs` opens the full output list at any width. Enter previews a result; Esc returns to the list, then chat; q closes the view. Chat always uses a single column, with no output sidebar or extra footer indicators.
- Ctrl+C keeps the existing two-press exit flow; Ctrl+Z/fg restores the terminal and input cursor in V2.

V2 shows `Running…` with elapsed time while the full response stream is pending.

User messages share the composer's surface and text alignment. Assistant text
uses a single bullet without repeated role/timestamp headers. Tools show an
action, target and a short output preview; **Ctrl+O** toggles input/output details.
Produced artifacts appear as compact `/outputs` entries below their source reply;
artifacts without a source association appear under Session outputs.
See the [V2 architecture and configuration guide](../../runtime/agentkit/docs/architecture-and-configuration.md)
for the frontend/backend boundary and workspace configuration.

A separate `Run completed`, `Run failed`, or `Interrupted — completion unknown`
summary measures request dispatch through stream completion using a monotonic clock;
queue waiting time is excluded. Errors retain their code and partial text, and no
request is replayed automatically. These display summaries are not checkpointed or
sent to the model; resumed history has no fabricated timings. Model configuration
still takes effect on restart; this migration adds no `/model` API or menu.

The visible cursor is the terminal's real cursor, positioned using grapheme and
terminal-cell widths. On iTerm2, Shift+Enter requires a distinguishable key sequence.
If it sends the same CR as Enter, bind it to “Send Escape Sequence” `[13;2u` (the
terminal adds ESC), or use Ctrl+J. IME candidate windows require a manual check with
your terminal/input-method settings. `NO_COLOR=1` disables colors; statuses retain
text and symbols.

## Migration verification

From the repository root:

```bash
npm --workspace @datafoundry/tui test
uv run --project runtime/agentkit --locked pytest runtime/agentkit/tests -m 'not live'
```

For the independent viewport checks, run `node apps/tui/test-chat-viewport.ts` on
Node with native TypeScript stripping (verified with Node 25.8.1), or use an already
installed `tsx` on older Node versions. No temporary package download is required.
The Python suite includes a real PTY probe for the locked Ink 7.1.0 fullscreen cursor
workaround, including an unpatched control. Run it before changing Ink: if upstream
fixes the one-row offset, remove the workaround instead of applying it twice.

See the [V2 architecture and configuration guide](../../runtime/agentkit/docs/architecture-and-configuration.md)
for verification entrypoints; IME candidate-window behavior still needs a manual check.
