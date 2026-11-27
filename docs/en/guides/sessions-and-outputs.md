# Sessions and outputs

A conversation has a session ID, `threadId` on HTTP or `thread_id` in the SDK. HTTP, TUI, and the SDK share the directory rules.

## Runtime state and outputs

Home stores the session index, input and output bindings, checkpoints, logs, and traces under `runtime/`. The default output directory is `<home>/outputs/<user>/<thread>/`. You can view, copy, or archive outputs. DataAgent manages its runtime state. See [Home layout](../../development/architecture/home-layout.md) and [permissions and ownership](../../development/architecture/state-and-storage.md#permissions-and-ownership).

## Sessions are identified by Home, user, and thread

- The local user ID defaults to `default`. Select it with `--user` or `LaunchOptions(user_id=...)`. It is distinct from a login account.
- User and thread IDs contain 1–128 characters. They start with an ASCII letter or digit and may also contain underscores and hyphens after that.

Each Home has its own session index. For example:

```text
~/.dataagent/runtime/users/alice/sessions/task-a/
/data/agent-home/runtime/users/alice/sessions/task-a/
```

These are separate sessions. Resume a conversation with the Home and user that created it. Within a Home, the server resolves ownership by `threadId`. If another user owns the ID, resume as that user or start a new session. The SDK binds by user and thread together. The host manages state isolation if callers share a checkpointer.

<a name="output-directory"></a>

## Output directory

For a new session, the first provided source wins:

| Priority | Source | Example value | Output for Alice's `task-a` |
| --- | --- | --- | --- |
| 1 | SDK argument `output_dir` | `/srv/reports/task-a` | `/srv/reports/task-a/` |
| 2 | `dataagent.output_root`, default `<home>/outputs` | `/srv/reports` | `/srv/reports/alice/task-a/` |

Omitting `output_root` or setting it to `null` uses `<home>/outputs`. DataAgent appends `<user>/<thread>/` to the root. An SDK `output_dir` is the final directory for one session, with no appended components.

Both accept absolute or `~/` paths. The latter resolves to the operating-system user's home directory. Relative forms such as `reports`, `./reports`, and `../reports` are rejected.

## One output directory per session

| Action | Result |
| --- | --- |
| Create a session | Resolve the directory in priority order and save it in `session_bindings` |
| Resume without `output_dir` | Use the saved directory |
| Resume with the same `output_dir` | Accept it after comparing resolved absolute paths |
| Resume with a different directory | Fail before execution and leave the binding and files in place |
| Change `output_root` or restart | Existing sessions keep their directories. The new setting applies to future sessions |
| Resume after the output directory was deleted | Recreate the bound directory empty |
| Switch directories | Use a new `thread_id` |

Exclusivity applies within one Home. Hosts coordinate directories across Homes.

## Directory ownership

Registration rejects identical paths, nested paths, and symbolic-link aliases that resolve to the same directory. The check also handles case differences on macOS and Windows. Outputs cannot overlap an input workspace. Inside Home, only `<home>/outputs` and its descendants can contain outputs. The check uses resolved paths. See [security boundaries](../../development/architecture/security-boundaries.md) for file and shell permissions.

| Directory state | Explicit SDK `output_dir` | Derived or default directory |
| --- | --- | --- |
| Missing or empty, no owner recorded | Create and register | Create and register |
| Already owned by this session | Reuse | Reuse |
| Identical to or nested with another bound directory | Error | Error |
| Nonempty, no owner recorded | Adopt and preserve files | Error |

An empty `artifacts/` does not count as existing user content. See [state and storage](../../development/architecture/state-and-storage.md#session-binding) for registration and concurrency.

<a name="common-errors"></a>

## Common errors

| Error | Resolution |
| --- | --- |
| `Output directory is used by session ...` | Choose a nonoverlapping directory in the same Home |
| `already contains files that no session owns` | Review the contents and use an explicit SDK `output_dir` to adopt them |
| `overlaps Home outside its outputs area` | Choose `<home>/outputs` or a separate directory outside Home |
| `overlaps an input workspace` | Separate inputs and outputs |
| `Session is already bound to a different output directory` | Reuse the directory or start a new session |

## Archive and move sessions

Preserve the corresponding Home state and output directory. Checkpoints can reference files at their original paths. Keep those locations available when moving session data. Hosts coordinate archives and access for external output directories across Homes.

## Input directories

Set defaults for new sessions through `dataagent.input_workspaces`. See [read-only workspaces](inputs.md#read-only-workspaces). A new session saves the input list in `session_bindings` and resumes with that list. YAML changes affect new sessions. The `name` setting is logical and is not a path component.

A custom host can supply a complete list to `create_agent(..., input_workspaces=...)`. An empty tuple `()` disables inputs. Omission or `None` uses YAML defaults for new sessions and stored bindings for existing ones. An explicit list for an existing session must match the bound paths and their order after resolution. Resume retains the original names. See the [host API](../reference/python-api.md#host-api).

## Resume

From source, resume directly:

```bash
npm run start:tui -- --resume <threadId>
```

You can also use `/resume` in the TUI. Installed clients can connect to a running server as described in the [TUI guide](tui.md).

To target an existing session on server startup:

```bash
uv run dataagent serve --session <threadId>
```

Resume keeps the same thread. It reads the checkpoint in the index when present, otherwise the latest graph state on that thread. Cancellation or disconnection marks the turn `interrupted`. Unfinished work is not replayed. Only one turn per session can run at once. Submit a new run and message ID for the next turn. See the [AG-UI request and resume contract](../reference/events.md#request). `/clear` opens a new thread while leaving the existing files on disk.

## View outputs

The session's output directory is the agent's default working directory. Offloaded tool results and conversation content live under `artifacts/large_tool_results` and `artifacts/conversation_history`. The listing endpoint skips both and does not follow symbolic links.

In TUI, `/outputs` opens the file list. Enter previews up to 64 KiB of UTF-8 text. Binary entries display metadata and can be opened locally. Listing failures show an error. Esc returns to the list and then chat. Use `q` to close the view.

See the [REST outputs endpoints](../reference/rest-api.md#outputs) for HTTP listing and preview. Browsing works if the original inputs are missing. Resuming execution requires readable inputs. Deleting offloaded files can break `read_file` references retained in checkpoints.
