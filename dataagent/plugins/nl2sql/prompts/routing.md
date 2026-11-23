### Configured database (nl2sql)

The **`nl2sql` subagent is the only way to query the configured database.** Its entry in the `task` tool description names the bound database, says when to use it, and explains how to read its replies.

- **Do not** search files, read configuration or logs, or run `sqlite3`/`mysql` in the shell to locate or query the configured database.
- This rule covers only the configured database. A database file that the user supplies as input is an ordinary input file: inspect it with your own tools.
