# TUI guide

This guide is for terminal users, remote server users, and developers. After reading it, you can start the TUI, restore sessions, and view runs and outputs in the terminal.

## How to start

With no extra flags it connects to `/dataagent/stream` from `config.json`: it starts `dataagent serve` when nothing is listening, and attaches when a control plane is already up. The old login and CopilotKit client are gone.

```bash
npm run start:tui
```

The address comes from Home `config.json` `server.host` and `server.port`, default `http://127.0.0.1:8790`. When a control plane is already listening there, the same command attaches and does not start a second backend. A control plane started with `npm run start -- --config` on another file's port is not discovered; if the Home address is free, this command starts a second backend from Home. Attach to the running one with `--runtime-url`. `./deploy.sh tui` connects to the API address recorded by `./deploy.sh start`. `--runtime-url` only attaches. Its host may be `server.host`, a loopback address, or another IP. `0.0.0.0` and `::` connect through loopback.

Resume the latest server session:

```bash
npm run start:tui -- --resume
```

Resume a specific thread/session:

```bash
npm run start:tui -- --resume thread-001
```

View CLI flags:

```bash
npm run start:tui -- --help
```

## Main Screen

The TUI opens in Chat. Use `/outputs` to open the current session outputs in a separate full-screen page, and `Esc` or `q` to close it.

## Slash commands

Type `/` and use `Tab` to complete. Built-in commands:

| Command | Action | Example |
| --- | --- | --- |
| `/help` | List available commands. | `/help` |
| `/clear` | Start a new conversation. Saved sessions stay on disk. | `/clear` |
| `/outputs` | Open the outputs page for the current session. | `/outputs` |
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
| `Enter` | Send message or run command. |

## Runtime behavior

The TUI sends natural-language input to the control plane's `/dataagent/stream` and shares the session list with the Web UI. Session outputs are available through `/outputs`.

`/resume` calls `GET /sessions` on the stream origin (`http://<server.host>:<server.port>`) with no authentication. There is no login prompt.

## Typical flow

1. Start the TUI. It launches the local backend.
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
| Configuration | Cannot be edited in the page. Both use the `config.json` loaded when the control plane started. | Same. `/resume` restores a session and `/outputs` lists files. |

Use the Web workbench for full visual demos. Use the TUI to verify agent runtime over SSH or lightweight terminal environments.

## Troubleshooting

- Cannot connect: run `npm run start:tui`. It uses Home `config.json` `server.host` and `server.port`, default `http://127.0.0.1:8790`, and starts a backend when nothing is listening there. For a control plane already started with another `--config`, pass `--runtime-url`. For a stack managed by `./deploy.sh`, run `./deploy.sh tui`; it uses the API address recorded at start.
- Attach without starting: pass `--runtime-url`. The host may be `server.host`, a loopback address, or another IP. `0.0.0.0` and `::` connect through loopback.
- Model not responding: check `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY` in root `.env`.
- Session restore fails: confirm `GET /sessions` on the stream origin (`http://<server.host>:<server.port>`) answers. The TUI sends that request with no authentication.
- Command has no effect: run `/help` and check errors in the command hint area.

Continue with [Web workbench guide](web-workbench.md).
