# TUI guide

This guide is for terminal users, remote server users, and developers. After reading it, you can start the TUI, restore sessions, and view runs and outputs in the terminal.

## How to start

After installing the TUI npm package, use `dataagent-cli --runtime-url <Server URL>` or `da --runtime-url <Server URL>` to connect to a running Server. See [DataAgent package deployment](deploy-packages.md#connect-tui).

When started from source, the TUI connects to `/api/v1/runs` at the address from `config.yaml` and starts `dataagent serve` if nothing is listening there.

```bash
npm run start:tui
```

The address comes from Home `config.yaml` `server.host` and `server.port`, default `http://127.0.0.1:8790`. When a control plane is already listening there, the same command attaches and does not start a second backend. A control plane started with `npm run start -- --config` on another file's port is not discovered; if the Home address is free, this command starts a second backend from Home. Attach to the running one with `--runtime-url`. `--runtime-url` only attaches. Its host may be `server.host`, a loopback address, or another IP. `0.0.0.0` and `::` connect through loopback.

`./deploy.sh tui` attaches to an existing backend. It uses `--runtime-url` first, then the saved API address from the last managed start, then the Home configuration. For example, run `./deploy.sh tui --runtime-url http://127.0.0.1:8791`.

Resume the latest server session:

```bash
npm run start:tui -- --resume
```

Resume a specific thread/session:

```bash
npm run start:tui -- --resume thread-001
```

From a source checkout, passing any of `--config`, `--env-file`, `--user`, or `--session` starts a new backend process and forwards those options to it. An explicit `--config` selects that YAML in place of Home `config.yaml`. These flags cannot be combined with `--runtime-url`.

When the source TUI starts a backend, it loads the repository root `.env` into the process environment. Existing terminal variables take precedence over that file. Non-empty process values then override the backend's selected environment file, which is Home `.env` by default or the file passed with `--env-file`. Connecting with `--runtime-url` uses the running backend's configuration.

Exiting the TUI stops the backend it started. A backend that was already running stays up.

View CLI flags:

```bash
npm run start:tui -- --help
```

## Main Screen

The TUI opens in Chat. Use `/outputs` to open the current session outputs in a separate full-screen page, and `Esc` or `q` to close it. The input box grows up to six visible rows, then scrolls internally.

## Slash commands

Type `/` and use `Tab` to complete. Built-in commands:

| Command | Action | Example |
| --- | --- | --- |
| `/help` | List available commands. Aliases are `h` and `?`. | `/help` |
| `/clear` | Start a new conversation. Saved sessions stay on disk. | `/clear` |
| `/stop` | Stop the current turn. Calls `POST /api/v1/runs/{runId}/cancel`, then aborts the local stream. The session stays on the same thread. | `/stop` |
| `/outputs` | Open the outputs page for the current session. Alias is `output`. | `/outputs` |
| `/resume` | Open the session picker. | `/resume` |
| `/resume list` | Open the session picker. | `/resume list` |
| `/resume latest` | Restore the most recent server session. | `/resume latest` |
| `/resume <id>` | Restore that session. | `/resume thread-001` |
| `/exit` | Exit the TUI. | `/exit` |

## Shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+C` | Clear current input; press again within 1 second to exit. |
| `Ctrl+L` | Clear chat display. |
| `Ctrl+N` | New session. |
| `PageUp` / `PageDown` | Scroll in Chat view. |
| `Home` / `End` | Jump to top or bottom of Chat scroll area. |
| Terminal paste shortcut | Paste text; content over 1000 characters or 10 lines is folded in the composer and expanded when sent. |
| `Tab` | Complete commands in the input box. |
| `↑` / `↓` | Move through multiline input first, then snap to the start/end before browsing history; the original draft and history are retained. |
| `Ctrl+U` | Clear current input. |
| `Ctrl+W` | Delete the previous word in input. |
| `Enter` | Send a message or run a command. |
| `Shift+Enter`, `Alt+Enter`, or `Ctrl+J` | Insert a newline. |
| `Ctrl+O` | Toggle tool input and output details. |
| `Ctrl+Z`, then `fg` (Linux and macOS) | Suspend and resume the TUI with the input cursor restored. |

## Runtime behavior

The TUI sends natural-language input to the control plane's `/api/v1/runs` and shares the session list with the Web UI. Session outputs are available through `/outputs`.

`/resume` calls `GET /api/v1/sessions` on the stream origin (`http://<server.host>:<server.port>`) with no authentication. There is no login prompt. Restore stays on the `threadId` the server returns. `/stop` uses the same cancel route as the Web UI.

Messages submitted during a run are queued. The Chat view shows `Running…` while the response stream is active, then reports completion, failure, or interruption. The elapsed time excludes queue time and appears only in the TUI for the live run. Restoring a session does not reconstruct previous timings.

## Typical flow

1. Start the TUI using one of the connection methods above.
2. Ask a question:

```text
In three sentences, say what you can do right now.
```

3. Watch streaming replies and tool calls in Chat.
4. Run `/outputs` for outputs.
5. Run `/resume` or `/resume list` to open the session picker. Use `/resume latest` for the newest session, or `/resume <id>` for a specific one.

## Compared with the Web workbench

| Dimension | Web workbench | TUI |
| --- | --- | --- |
| Environment | Browser, local demos, business analysis. | SSH, remote servers, terminal workflows. |
| Interaction | Clicks, input box, console. | Keyboard and slash commands. |
| Trace | Right console, step details, trace list. | Chat transcript and `/outputs` page. |
| Configuration | Cannot be edited in the page. Both use the `config.yaml` loaded when the control plane started. | Same. `/resume` restores a session and `/outputs` lists files. |

Use the Web workbench for full visual demos. Use the TUI to verify agent runtime over SSH or lightweight terminal environments.

## Troubleshooting

- Cannot connect: run `npm run start:tui`. It uses Home `config.yaml` `server.host` and `server.port`, default `http://127.0.0.1:8790`, and starts a backend when nothing is listening there. For a control plane already started with another `--config`, pass `--runtime-url`. For a stack managed by `./deploy.sh`, run `./deploy.sh tui`; it uses the API address recorded at start.
- Attach without starting: pass `--runtime-url`. The host may be `server.host`, a loopback address, or another IP. `0.0.0.0` and `::` connect through loopback.
- Model not responding: check `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY` in the running backend's environment or selected env file.
- `Shift+Enter` submits in iTerm2: bind it to “Send Escape Sequence” with `[13;2u` or use `Ctrl+J`. Check input method candidate windows in your terminal.
- Color or connection diagnostics: set `NO_COLOR=1` in the TUI environment to turn off color. The TUI writes logs to `runtime/logs/tui.log` in the client's DataAgent Home.
- Session restore fails: confirm `GET /api/v1/sessions` on the stream origin (`http://<server.host>:<server.port>`) answers. The TUI sends that request with no authentication.
- Command has no effect: run `/help` and check errors in the command hint area.

Continue with [Web workbench guide](web.md).
