# DataAgent Session Analyzer

Offline trajectory & log analysis tool for DataAgent sessions. Reads persisted session artifacts (trajectory graphs, messages, logs) and generates an interactive HTML report with charts, timelines, and error diagnostics.

## Quick Start

```bash
# Analyze a session by path
python -m scripts.analyzer --session ~/.dataagent/user123/session_abc

# Analyze by user + session ID
python -m scripts.analyzer --user-id user123 --session-id session_abc

# Output to a specific file
python -m scripts.analyzer --session ~/.dataagent/user123/session_abc --output report.html

# Batch analyze all sessions for a user
python -m scripts.analyzer --user-id user123 --all
```

Open the generated `report.html` in any browser — no server needed.

## CLI Reference

| Flag | Short | Description |
|---|---|---|
| `--session` | `-s` | Path to session directory |
| `--user-id` | `-u` | DataAgent user ID (auto-locates `~/.dataagent/{user_id}/{session_id}`) |
| `--session-id` | `-i` | DataAgent session ID |
| `--all` | `-a` | Analyze all sessions under a user |
| `--user` | | Analyze every session under `~/.dataagent/{user}` or an explicit user directory |
| `--output` | `-o` | Output HTML path (default: `<session>/report.html`) |
| `--log-dir` | | Override log directory (default: auto-detect) |
| `--logs-only` | | Run only log analysis |
| `--trajectory-only` | | Run only trajectory analysis |
| `--workers` | | Batch worker count (default: available CPU count) |
| `--override` | | Rewrite existing reports; use `false` to skip them while backfilling missing ZIPs |

## Programmatic API

```python
from scripts.analyzer import generate_report

html = generate_report(
    session_root=Path("~/.dataagent/user123/session_abc").expanduser(),
    output=Path("report.html"),
)
```

## Report Features

### Dashboard
Overview cards showing turn count, runs, tool calls, failures, log errors, and warnings. Session duration badge when log timestamps are available.

### Tool Call Statistics
- **Stacked bar chart** — per-tool call count with success/failure breakdown (Chart.js)
- **Donut chart** — overall success rate
- **Node type distribution** — horizontal bar chart of all node types in the trajectory

### Failed Actions Table
Searchable, sortable table of all failed tool calls with full params and output. Error keywords are highlighted inline.

### Timeline
Each trajectory entry (State / Action / Query) rendered chronologically:

- **State** — LLM response rendered as Markdown with `marked.js`
- **Action** — collapsible detail showing params, output, and associated DataNodes
- **Timestamps** — all entries show `HH:MM:SS.fff` on the right
- **Latency** — State shows time since previous turn; Actions show time since their parent State (actions under the same State are parallel, so latency is relative to the parent, not chained)

Timestamps are resolved from log entries:
- States match `LLM stream finished` log lines (LLM completion time)
- Actions match `Context: Modifying node=Action(...)` log lines (final persistence time)
- Falls back to estimation when exact matches are unavailable

### Context Growth
Git-commit-style round-by-round view of message accumulation in the LLM context window. Shows queries, tool calls, tool results, and compressed responses with cumulative message counts.

### Log Analysis
- ERROR and WARNING entries filtered by session time window
- Session-level log file matching (by session ID and trajectory node IDs)
- Main and subagent logs are resolved under `~/.dataagent/{user_id}/logs`
- Expandable log entries with full messages
- Search and level filter

### Subagent Layouts

The analyzer supports these layouts in priority order:

1. A parent `.performance/Run<run>_Sub<nonzero>.<pid>.jsonl` file identifies a subagent sharing the parent workspace.
   Matching `.context/Run<run>_Sub<nonzero>.json` files form that logical subagent's trajectory.
2. Otherwise, `<session_root>/subagents/<subagent_session>/` is an independent subagent workspace and session root.
3. Without either signal, `.context/Run<run>_Sub<nonzero>.json` files are treated as historical inline subagents.
4. Historical sibling directories named `subagent_<parent_session_id>_<sub_id>` remain supported as a fallback.

Inline reports use an explicit artifact scope. Context files are selected by `sub_id`; new performance files named
`Run<run_id>_Sub<sub_id>.<pid>.jsonl` are selected by `sub_id` across all runs. Main-agent Time and Token analysis uses
only `Sub0` files. Historical files fall back to the subagent log's
`[perf] enabled, jsonl=...` line and then exclusive `run_id` matching. Ambiguous shared performance is withheld and
reported as unavailable instead of being mixed into the parent or child totals.

## Architecture

```
scripts/analyzer/
├── __init__.py          # Public API, generate_report(), auto-registration
├── __main__.py          # python -m scripts.analyzer entry point
├── base.py              # BaseAnalyzer (ABC) + AnalyzerRegistry
├── cli.py               # argparse CLI
├── logs.py              # LogAnalyzer — Loguru log parsing
├── performance.py       # Shared performance JSONL reader
├── report.py            # HTMLReportGenerator — Jinja2 + Chart.js rendering
├── scope.py              # Artifact ownership for inline logical sessions
├── subagents.py          # Physical/inline/legacy subagent discovery
├── time.py               # TimeAnalyzer
├── token.py              # TokenAnalyzer
├── trajectory.py        # TrajectoryAnalyzer — graph parsing + stats
└── templates/
    └── report.html      # Jinja2 template (self-contained HTML)
```

### Data Flow

```
Session Directory (~/.dataagent/{user}/{session})
    │
    ├── .context/Run*.json    →  TrajectoryAnalyzer  →  tool_stats, timeline, failures
    ├── .memory/messages.json →  TrajectoryAnalyzer  →  context_growth
    ├── .performance/*.jsonl  →  Time/Token Analyzer →  latency, tokens
    ├── subagents/*           →  SubagentAnalyzer    →  independent child reports
    └── ../logs/*.log         →  LogAnalyzer         →  errors, warnings, timestamps
                                        │
                                        ▼
                                 HTMLReportGenerator (Jinja2)
                                        │
                                        ▼
                                   report.html
                              (Chart.js + marked.js CDN)
```

### Extensibility

Each analyzer implements `BaseAnalyzer` and registers with `AnalyzerRegistry`. To add a new analyzer:

```python
from scripts.analyzer.base import BaseAnalyzer, AnalyzerRegistry

class CostAnalyzer(BaseAnalyzer):
    name = "cost"
    description = "Token cost analysis"

    def analyze(self, session_root, **kwargs):
        # Read session data, return dict
        return {"total_tokens": 15000, "cost": 0.03}

AnalyzerRegistry.register(CostAnalyzer())
```

Then add a corresponding section block in `templates/report.html`. The HTML report generator picks up registered analyzers by name.

## Dependencies

All dependencies are already in DataAgent's core requirements:
- **Jinja2** — HTML templating (already a DataAgent dependency)
- **Chart.js** — loaded via CDN in the browser (no server-side dependency)
- **marked.js** — Markdown rendering via CDN (no server-side dependency)

No additional Python packages required.
