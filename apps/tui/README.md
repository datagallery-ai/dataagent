# DataGallery TUI (`@datagallery/tui`)

Terminal interface for the datagallery monorepo.

- **User guide:** [TUI 指南](../../docs/zh/guides/tui.md)
- **Quick start:** [快速开始](../../docs/zh/quick-start.md)

From the repository root:

```bash
npm install
npm run start:tui
```

`npm run start:tui` connects to `/dataagent/stream` at the address in Home `config.json` `server.host` and `server.port`, default `http://127.0.0.1:8790`. Nothing listening there starts `dataagent serve`; a control plane that is already up is reused.

## Home

Home defaults live in `~/.dataagent`. Add `--resume` or `--resume <threadId>` to restore a session. `--config` selects that file instead of Home `config.json`. `--config`, `--env-file`, `--workspace`, `--user` and `--session` are forwarded to a backend this TUI starts, so they do not attach to one that is already running.

`--runtime-url` attaches and does not start a backend. The host may be `server.host` from `config.json`, a loopback address, or another IP. `0.0.0.0` and `::` connect through loopback.

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8790
```

There is no project configuration layer or trust prompt. Logs/checkpoints live in
`<home>/runtime`; session outputs live in `runtime/users/<user>/sessions/<session>/outputs`.
Existing session input bindings are retained; no files are migrated or deleted. TUI theme
and log variables stay in the TUI process environment. `--env-file` is forwarded to the backend that this TUI starts.

See the [configuration and plugin guide](../../runtime/agentkit/README.md).

## Chat and keyboard behavior

The composer starts with one input row and grows upward to six visible rows
(fewer on short terminals), then scrolls internally. Its borderless gray surface
and model/workspace footer use the full chat width. The normal empty control area
is four rows. Both home and chat support
60-column terminals; the minimum height is 19 rows.

- Enter submits. Shift+Enter (CSI-u or xterm modifyOtherKeys), Alt+Enter and Ctrl+J insert a newline.
- Multi-line bracketed paste stays one editable draft; large pastes keep the existing fold/expand behavior.
- Up/Down navigate the draft and command history. Messages entered during a run remain queued.
- `/` opens commands and Tab completes. The TUI exposes `/help`, `/clear`, `/stop`, `/resume`, `/resume list`, `/resume latest`, `/resume <id>`, `/outputs` and `/exit`. `/stop` cancels the active turn through `POST /api/v1/runs/{runId}/cancel` and stays on the same thread. `/resume` and `/resume list` open the session picker and restore that thread.
- `/outputs` opens the full output list at any width. Enter previews a result; Esc returns to the list, then chat; q closes the view. Chat always uses a single column, with no output sidebar or extra footer indicators.
- Ctrl+C keeps the existing two-press exit flow; Ctrl+Z/fg restores the terminal and input cursor in the TUI.

The TUI shows `Running…` with elapsed time while the full response stream is pending.

User messages share the composer's surface and text alignment. Assistant text
uses a single bullet without repeated role/timestamp headers. Tools show an
action, target and a short output preview; **Ctrl+O** toggles input/output details.
Produced artifacts appear as compact `/outputs` entries below their source reply;
artifacts without a source association appear under Session outputs.
See the [AgentKit docs](../../runtime/agentkit/docs/README.md)
for the frontend/backend boundary and workspace configuration.

A separate `Run completed`, `Run failed`, or `Interrupted — continue this conversation`
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
npm run test:tui
uv run --project runtime/agentkit --locked pytest runtime/agentkit/tests -m 'not live'
```

For the independent viewport checks, run `node apps/tui/test-chat-viewport.ts` on
Node with native TypeScript stripping (verified with Node 25.8.1), or use an already
installed `tsx` on older Node versions. No temporary package download is required.
The Python suite includes a real PTY probe for the locked Ink 7.1.0 fullscreen cursor
workaround, including an unpatched control. Run it before changing Ink: if upstream
fixes the one-row offset, remove the workaround instead of applying it twice.

See the [AgentKit docs](../../runtime/agentkit/docs/README.md)
for verification entrypoints; IME candidate-window behavior still needs a manual check.
