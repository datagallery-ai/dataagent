# TUI guide

This guide is for terminal users, remote server users, and developers. After reading it, you can start the TUI, connect to the backend, select data sources or Skills, restore server sessions, and view runs and outputs in the terminal.

## How to start

Start the full dev stack or backend first:

```bash
npm run dev
```

Start the TUI:

```bash
npm run start:tui
```

Point at a specific runtime URL:

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8787/api/copilotkit
```

Set default data source and agent name:

```bash
npm run start:tui -- --datasource-id dtc-growth-demo --agent dataFoundry
```

Resume the latest server session:

```bash
npm run start:tui -- --resume
```

Resume a specific thread/session:

```bash
npm run start:tui -- --resume thread-001
```

The TUI requires a running API and password sign-in (offline demo mode was removed):

```bash
npm run start:tui -- --runtime-url http://127.0.0.1:8787/api/copilotkit
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
| `/clear` | Clear current chat display. | `/clear` |
| `/status` | Show thread, message count, current data source and Skill. | `/status` |
| `/outputs` | Open the outputs page for the current session. | `/outputs` |
| `/datasource` | Open the data source picker. | `/datasource` |
| `/skill` | Open Skill picker, list, or select a Skill. | `/skill show` |
| `/reset` | Create a new local session. | `/reset` |
| `/resume [latest\|list\|sessionId]` | Restore a server session. | `/resume list` |
| `/exit` | Exit the TUI. | `/exit` |

`/datasource` usage:

```text
/datasource
```

`/skill` usage:

```text
/skill
/skill show
/skill current
/skill select <id>
/skill <id>
/<skill-id>
```

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

When connected to a real backend, the TUI sends natural-language input to `/api/copilotkit` and writes the current data source, enabled resources, and Skill selection into `run_config`. AG-UI events from the backend appear in Chat as text and tool calls; session outputs are available through `/outputs`.

`/resume` depends on `/api/v1/sessions` and `/api/v1/sessions/:id/conversation`. If the backend is unavailable or sessions are unsupported, the TUI shows an error in the command hint area.

Offline demo mode has been removed. The TUI always requires a running API and password sign-in.

## Typical flow

1. Start backend and TUI.
2. Run `/status` to see thread, data source, and Skill.
3. Run `/datasource` to open the data source picker.
4. When needed, select `dtc-growth-demo` in the picker and press Enter.
5. Ask a question:

```text
List tables in the current data source and compute GMV by channel from the orders table.
```

6. Watch streaming replies and tool calls in Chat.
7. Run `/outputs` for outputs.

## Compared with the Web workbench

| Dimension | Web workbench | TUI |
| --- | --- | --- |
| Environment | Browser, local demos, business analysis. | SSH, remote servers, terminal workflows. |
| Interaction | Clicks, input box, console. | Keyboard and slash commands. |
| Trace | Right console, step details, trace list. | Chat transcript and `/outputs` page. |
| Resources | Forms for create, test, import, preview. | Select data source and Skill; view config state. |

Use the Web workbench for full visual demos. Use the TUI to verify agent runtime over SSH or lightweight terminal environments.

## Troubleshooting

- Cannot connect: confirm `npm run dev` or `npm run dev:api` is running.
- Backend URL changed: pass full `/api/copilotkit` URL with `--runtime-url`.
- Model not responding: check `LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, and `LLM_API_KEY` in root `.env`.
- Session restore fails: confirm `/api/v1/sessions` is reachable and that you are signed in against the same `--runtime-url`.
- Command has no effect: run `/help` and check errors in the command hint area.

Continue with [Web workbench guide](web-workbench.md).
