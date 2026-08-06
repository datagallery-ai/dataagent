#!/usr/bin/env python3
"""Shared helpers for Bird Cursor eval (run + grade)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# cursor-sdk bridge uses os.get_blocking/set_blocking (Unix / newer Python).
# On Windows these may be missing — patch before importing cursor_sdk.
if not hasattr(os, "get_blocking"):
    os.get_blocking = lambda fd: True  # type: ignore[attr-defined]
if not hasattr(os, "set_blocking"):
    os.set_blocking = lambda fd, blocking: None  # type: ignore[attr-defined]

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EvalConfig:
    cursor_api_key: str = ""
    model: str = "composer-2.5"
    cwd: str = "."
    mcp_url: str = "http://127.0.0.1:8080/mcp"
    dataset: str = "evaluate/bird/dev_questions.json"
    answers: str = "evaluate/bird/dev_answers.json"
    output_dir: str = "evaluate/bird/cursor/eval_results"
    # SQLite: use ONE of these patterns (set absolute paths in your local config)
    sqlite_url: str = ""  # single DB for all questions, e.g. sqlite:///C:/path/to/db.sqlite
    sqlite_url_template: str = "sqlite:///{db_dir}/{db_id}/{db_id}.sqlite"
    db_dir: str = ""
    sqlite_databases: dict[str, str] = field(default_factory=dict)
    limit: int = 0
    offset: int = 0
    difficulties: list[str] = field(default_factory=list)
    db_ids: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=lambda: ["with_datalink", "without_datalink"])
    timeout_seconds: int = 600
    # When True, Cursor local agent is sandboxed to cwd (blocks grep/glob/read outside).
    sandbox_enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvalConfig:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def resolve_api_key(self) -> str:
        key = self.cursor_api_key or os.environ.get("CURSOR_API_KEY", "")
        if not key:
            raise SystemExit(
                "Missing Cursor API key. Set CURSOR_API_KEY or pass cursor_api_key in config.\n"
                "Get one at: https://cursor.com/dashboard → Integrations → User API Keys"
            )
        return key

    def sqlite_url_for(self, db_id: str) -> str:
        if self.sqlite_url:
            return self.sqlite_url
        if db_id in self.sqlite_databases:
            return self.sqlite_databases[db_id]
        if not self.db_dir:
            raise ValueError(
                f"No SQLite URL for db_id={db_id!r}. "
                "Set sqlite_url, sqlite_databases[db_id], or db_dir + sqlite_url_template."
            )
        path = self.sqlite_url_template.format(db_dir=self.db_dir, db_id=db_id)
        return path


@dataclass
class ToolCallRecord:
    name: str
    status: str
    args: Any = None
    result: Any = None


@dataclass
class RunMetrics:
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_call_count: int = 0
    datalink_tool_calls: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


@dataclass
class RunResult:
    """Agent run output (no golden SQL / correctness)."""

    question_id: int
    db_id: str
    mode: str
    question: str
    predicted_sql: str | None
    error: str | None
    metrics: RunMetrics
    answer_text: str
    difficulty: str = ""
    run_id: str = ""
    agent_id: str = ""


@dataclass
class EvalResult:
    """Scored result (run output + golden comparison)."""

    question_id: int
    db_id: str
    mode: str
    question: str
    golden_sql: str
    predicted_sql: str | None
    correct: bool | None
    error: str | None
    golden_row_count: int | None
    predicted_row_count: int | None
    metrics: RunMetrics
    answer_text: str
    difficulty: str = ""
    run_id: str = ""
    agent_id: str = ""


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*\n(.*?)```", re.IGNORECASE | re.DOTALL)
_SELECT_RE = re.compile(r"\b(SELECT\b[\s\S]*?)(?:;|\n\n|$)", re.IGNORECASE)


def extract_sql(text_body: str) -> str | None:
    """Pull the most likely SQL statement from agent output."""
    if not text_body:
        return None

    fences = _SQL_FENCE_RE.findall(text_body)
    for block in reversed(fences):
        stmt = block.strip().rstrip(";")
        if stmt.upper().startswith("SELECT"):
            return stmt

    matches = _SELECT_RE.findall(text_body)
    if matches:
        return matches[-1].strip().rstrip(";")

    stripped = text_body.strip()
    if stripped.upper().startswith("SELECT"):
        return stripped.rstrip(";")
    return None


def _normalize_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _normalize_rows(rows: list[tuple]) -> list[tuple]:
    return [tuple(_normalize_cell(c) for c in row) for row in rows]


def execute_sql(engine: Engine, sql: str) -> tuple[list[tuple], str | None]:
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            rows = result.fetchall()
            return _normalize_rows([tuple(r) for r in rows]), None
    except Exception as exc:
        return [], str(exc)


def compare_results(golden_rows: list[tuple], pred_rows: list[tuple]) -> bool:
    """Multiset comparison (order-insensitive)."""
    from collections import Counter

    return Counter(golden_rows) == Counter(pred_rows)


def get_engine(db_id: str, config: EvalConfig, engine_cache: dict[str, Engine]) -> Engine:
    if db_id not in engine_cache:
        engine_cache[db_id] = create_engine(config.sqlite_url_for(db_id))
    return engine_cache[db_id]


# ---------------------------------------------------------------------------
# Cursor SDK runner
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """You are a text-to-SQL assistant. Answer the question by writing a single SQLite SQL query.

Database id: {db_id}
{sqlite_block}Question: {question}
{evidence_block}

Rules:
{path_rule}{explore_rule}- Do not use Grep or Glob (they are disabled for this evaluation).
- Use only tables/columns that exist in the database.
- Output exactly one SQL query inside a ```sql code fence.
- Do not execute the query yourself; only write the SQL.
- The query must be valid SQLite syntax.。
- You are only allowed to access files and data inside your working directory.
- Do not search online.
- Do not access git history.
{python_rule}"""


def sqlite_url_to_path(sqlite_url: str) -> str:
    """Turn sqlite:///... URL into a filesystem path for the prompt."""
    prefix = "sqlite:///"
    if sqlite_url.startswith(prefix):
        path = sqlite_url[len(prefix) :]
        # SQLAlchemy: sqlite:////C:/... (4 slashes) -> /C:/...; normalize drive paths.
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    return sqlite_url


def build_prompt(
    item: dict[str, Any],
    *,
    sqlite_url: str = "",
    inject_sqlite_path: bool = False,
    with_datalink: bool = False,
) -> str:
    evidence = item.get("evidence") or ""
    evidence_block = f"Hint: {evidence}" if evidence else ""
    if inject_sqlite_path and sqlite_url:
        sqlite_block = f"SQLite database file: {sqlite_url_to_path(sqlite_url)}\n"
        path_rule = (
            "- The database file path above is authoritative — do not search the "
            "workspace for other DB files.\n"
        )
    else:
        sqlite_block = ""
        path_rule = ""
    if with_datalink:
        explore_rule = (
            "- Do not use Python or bash to inspect or probe the database; "
            "use DataLink MCP tools (e.g. datalink_explore) instead.\n"
        )
        python_rule = ""
    else:
        explore_rule = ""
        python_rule = (
            "- The environment does not have sqlite3 command in the terminal. "
            "You have to use python if needed.\n"
        )
    return PROMPT_TEMPLATE.format(
        db_id=item["db_id"],
        sqlite_block=sqlite_block,
        question=item["question"],
        evidence_block=evidence_block,
        path_rule=path_rule,
        explore_rule=explore_rule,
        python_rule=python_rule,
    )


def _mcp_hosts(mcp_url: str) -> list[str]:
    """Hosts to allow in sandbox network policy for DataLink MCP."""
    hosts = {"127.0.0.1", "localhost"}
    if mcp_url:
        parsed = urlparse(mcp_url)
        if parsed.hostname:
            hosts.add(parsed.hostname)
    return sorted(hosts)


_DENY_SEARCH_HOOK_PY = '''\
#!/usr/bin/env python3
"""Deny Grep/Glob during Bird Cursor eval (preToolUse)."""
from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({"permission": "allow"}))
        return

    name = str(payload.get("tool_name") or "").strip()
    if name.lower() in {"grep", "glob"}:
        print(
            json.dumps(
                {
                    "permission": "deny",
                    "user_message": f"{name} is disabled for this evaluation.",
                    "agent_message": (
                        f"The {name} tool is disabled. Do not search the workspace "
                        "with Grep/Glob. Use DataLink MCP tools if available."
                    ),
                }
            )
        )
        return

    print(json.dumps({"permission": "allow"}))


if __name__ == "__main__":
    main()
'''

_DENY_SEARCH_HOOKS_JSON = {
    "version": 1,
    "hooks": {
        "preToolUse": [
            {
                "command": "python .cursor/hooks/deny-search.py",
                "failClosed": True,
            }
        ]
    },
}


def ensure_sandbox_policy(cwd: Path, *, mcp_url: str = "") -> Path:
    """Ensure cwd/.cursor/sandbox.json allows MCP hosts when sandbox is on.

    Does not remove existing allow entries; merges mcp hosts into networkPolicy.allow.
    """
    cursor_dir = cwd / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    path = cursor_dir / "sandbox.json"

    policy: dict[str, Any] = {
        "type": "workspace_readwrite",
        "additionalReadwritePaths": [],
        "additionalReadonlyPaths": [],
        "networkPolicy": {
            "default": "deny",
            "allow": _mcp_hosts(mcp_url),
        },
    }
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                policy = {**policy, **existing}
                net = policy.setdefault("networkPolicy", {})
                if not isinstance(net, dict):
                    net = {}
                    policy["networkPolicy"] = net
                allow = list(net.get("allow") or [])
                for h in _mcp_hosts(mcp_url):
                    if h not in allow:
                        allow.append(h)
                net["allow"] = allow
                net.setdefault("default", "deny")
        except (json.JSONDecodeError, OSError):
            pass

    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    return path


def ensure_eval_hooks(cwd: Path) -> Path:
    """Install preToolUse hooks that deny Grep/Glob under cwd/.cursor/."""
    hooks_dir = cwd / ".cursor" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script_path = hooks_dir / "deny-search.py"
    script_path.write_text(_DENY_SEARCH_HOOK_PY, encoding="utf-8")
    hooks_json = cwd / ".cursor" / "hooks.json"
    hooks_json.write_text(
        json.dumps(_DENY_SEARCH_HOOKS_JSON, indent=2) + "\n",
        encoding="utf-8",
    )
    return hooks_json


_KEEP_SUFFIXES = {".sqlite", ".db"}


def reset_sandbox_cwd(cwd: Path) -> list[str]:
    """Remove agent leftovers under cwd; keep DB files and `.cursor/`.

    Returns relative paths that were deleted (for logging).
    """
    import shutil

    cwd = cwd.resolve()
    if not cwd.is_dir():
        cwd.mkdir(parents=True, exist_ok=True)
        return []

    removed: list[str] = []
    for entry in list(cwd.iterdir()):
        name = entry.name
        if name == ".cursor":
            continue
        if entry.is_file() and entry.suffix.lower() in _KEEP_SUFFIXES:
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed.append(name)
        except OSError:
            pass
    return removed


def run_cursor_agent(
    *,
    prompt: str,
    config: EvalConfig,
    with_datalink: bool,
) -> tuple[str, RunMetrics, str, str]:
    from cursor_sdk import (
        Agent,
        AgentOptions,
        HttpMcpServerConfig,
        LocalAgentOptions,
        SandboxOptions,
    )

    mcp_servers: dict[str, Any] = {}
    if with_datalink:
        mcp_servers = {
            "datalink": HttpMcpServerConfig(url=config.mcp_url, type="http"),
        }

    cwd_path = Path(config.cwd).resolve()
    cwd = str(cwd_path)
    api_key = config.resolve_api_key()

    removed = reset_sandbox_cwd(cwd_path)
    if removed:
        print(f"  sandbox reset: removed {removed}")

    sandbox_options = None
    ensure_eval_hooks(cwd_path)
    if config.sandbox_enabled:
        ensure_sandbox_policy(cwd_path, mcp_url=config.mcp_url if with_datalink else "")
        sandbox_options = SandboxOptions(enabled=True)

    metrics = RunMetrics()
    answer_text = ""
    run_id = ""
    agent_id = ""

    with Agent.create(
        AgentOptions(
            api_key=api_key,
            model=config.model,
            local=LocalAgentOptions(
                cwd=cwd,
                # Need "project" so cwd/.cursor/hooks.json (deny Grep/Glob) is loaded.
                # Do not include "user"/"plugins" — avoids ambient MCP/rules outside the sandbox.
                setting_sources=["project"],
                sandbox_options=sandbox_options,
            ),
            mcp_servers=mcp_servers or None,
        )
    ) as agent:
        agent_id = agent.agent_id
        run = agent.send(prompt)
        run_id = run.id

        for msg in run.messages():
            if msg.type == "tool_call" and msg.status == "completed":
                # Cursor MCP tools show as name="mcp"; real tool is in args.toolName.
                display_name = msg.name or ""
                args = msg.args
                if isinstance(args, dict):
                    tool_name = args.get("toolName") or args.get("tool_name") or ""
                    provider = args.get("providerIdentifier") or args.get("provider") or ""
                    if tool_name:
                        display_name = str(tool_name)
                else:
                    tool_name = ""
                    provider = ""

                rec = ToolCallRecord(
                    name=display_name,
                    status=msg.status,
                    args=args,
                    result=msg.result,
                )
                metrics.tool_calls.append(rec)
                metrics.tool_call_count += 1
                blob = f"{msg.name} {display_name} {provider} {tool_name}".lower()
                if "datalink" in blob:
                    metrics.datalink_tool_calls += 1
            elif msg.type == "usage" and msg.usage is not None:
                metrics.input_tokens = msg.usage.input_tokens
                metrics.output_tokens = msg.usage.output_tokens
                metrics.total_tokens = msg.usage.total_tokens

        final = run.wait()
        answer_text = final.result or ""
        metrics.duration_ms = final.duration_ms or 0
        if final.usage is not None:
            metrics.input_tokens = final.usage.input_tokens
            metrics.output_tokens = final.usage.output_tokens
            metrics.total_tokens = final.usage.total_tokens

    return answer_text, metrics, run_id, agent_id


# ---------------------------------------------------------------------------
# Dataset / config IO
# ---------------------------------------------------------------------------


def load_json_list(path: str | Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array: {path}")
    return data


def load_answers(path: str | Path) -> dict[tuple[Any, str], dict[str, Any]]:
    """Index answers by (question_id, db_id)."""
    items = load_json_list(path)
    out: dict[tuple[Any, str], dict[str, Any]] = {}
    for item in items:
        if "SQL" not in item:
            raise ValueError(f"Answer entry missing SQL: {item!r}")
        key = (item.get("question_id"), str(item.get("db_id", "")))
        out[key] = item
    return out


def filter_dataset(items: list[dict[str, Any]], config: EvalConfig) -> list[dict[str, Any]]:
    out = items
    if config.difficulties:
        allowed = {d.lower() for d in config.difficulties}
        out = [x for x in out if x.get("difficulty", "").lower() in allowed]
    if config.db_ids:
        allowed = set(config.db_ids)
        out = [x for x in out if x.get("db_id") in allowed]
    if config.offset:
        out = out[config.offset :]
    if config.limit:
        out = out[: config.limit]
    return out


def load_config(config_path: Path | None) -> EvalConfig:
    if config_path is not None:
        with open(config_path, encoding="utf-8") as f:
            return EvalConfig.from_dict(json.load(f))
    return EvalConfig()


def resolve_config_path(args_config: str | None, script_file: str) -> Path | None:
    """Return config path: --config if set, else sibling eval.config.json if it exists."""
    if args_config:
        return Path(args_config)
    default = Path(script_file).resolve().parent / "eval.config.json"
    return default if default.is_file() else None


def repo_root_from(script_file: str) -> Path:
    # evaluate/bird/cursor/<script>.py → repo root
    return Path(script_file).resolve().parents[3]


def metrics_from_dict(d: dict[str, Any] | None) -> RunMetrics:
    m = d or {}
    tool_calls = [
        ToolCallRecord(
            name=tc.get("name", ""),
            status=tc.get("status", ""),
            args=tc.get("args"),
            result=tc.get("result"),
        )
        for tc in (m.get("tool_calls") or [])
    ]
    return RunMetrics(
        duration_ms=m.get("duration_ms", 0),
        input_tokens=m.get("input_tokens", 0),
        output_tokens=m.get("output_tokens", 0),
        total_tokens=m.get("total_tokens", 0),
        tool_call_count=m.get("tool_call_count", 0),
        datalink_tool_calls=m.get("datalink_tool_calls", 0),
        tool_calls=tool_calls,
    )


def eval_result_from_dict(d: dict[str, Any]) -> EvalResult:
    return EvalResult(
        question_id=d["question_id"],
        db_id=d["db_id"],
        mode=d["mode"],
        question=d.get("question", ""),
        golden_sql=d.get("golden_sql", ""),
        predicted_sql=d.get("predicted_sql"),
        correct=d.get("correct"),
        error=d.get("error"),
        golden_row_count=d.get("golden_row_count"),
        predicted_row_count=d.get("predicted_row_count"),
        metrics=metrics_from_dict(d.get("metrics")),
        answer_text=d.get("answer_text", ""),
        difficulty=d.get("difficulty", ""),
        run_id=d.get("run_id", ""),
        agent_id=d.get("agent_id", ""),
    )


def result_to_dict(r: RunResult | EvalResult) -> dict[str, Any]:
    return asdict(r)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

_DIFFICULTY_ORDER = ("simple", "moderate", "challenging")


def _difficulty_sort_key(name: str) -> tuple[int, str]:
    lowered = name.lower()
    try:
        return (_DIFFICULTY_ORDER.index(lowered), lowered)
    except ValueError:
        return (len(_DIFFICULTY_ORDER), lowered)


def _group_avgs(rows: list[EvalResult]) -> dict[str, float]:
    total = len(rows)
    if not total:
        return {"tools": 0.0, "tokens": 0.0, "duration_ms": 0.0, "datalink": 0.0}
    return {
        "tools": sum(r.metrics.tool_call_count for r in rows) / total,
        "tokens": sum(r.metrics.total_tokens for r in rows) / total,
        "duration_ms": sum(r.metrics.duration_ms for r in rows) / total,
        "datalink": sum(r.metrics.datalink_tool_calls for r in rows) / total,
    }


def _format_group_stats(label: str, rows: list[EvalResult]) -> list[str]:
    scored = [r for r in rows if r.correct is not None]
    correct = sum(1 for r in scored if r.correct)
    acc = correct / len(scored) if scored else 0.0
    avgs = _group_avgs(rows)
    return [
        f"[{label}]",
        f"  accuracy:     {correct}/{len(scored)} = {acc:.1%}",
        f"  avg tools:    {avgs['tools']:.1f}",
        f"  avg datalink: {avgs['datalink']:.1f}",
        f"  avg tokens:   {avgs['tokens']:.0f}",
        f"  avg duration: {avgs['duration_ms']:.0f} ms",
    ]


def _pct_change(with_val: float, without_val: float) -> str:
    """Relative change of with vs without. Negative = lower cost with DataLink."""
    if without_val == 0:
        return "n/a" if with_val == 0 else "+inf"
    pct = (with_val - without_val) / without_val * 100
    return f"{pct:+.1f}%"


def _format_datalink_improvement(
    with_rows: list[EvalResult],
    without_rows: list[EvalResult],
) -> list[str]:
    """Compare with_datalink vs without_datalink for tools / tokens / duration."""

    def by_difficulty(rows: list[EvalResult]) -> dict[str, list[EvalResult]]:
        out: dict[str, list[EvalResult]] = {}
        for r in rows:
            key = r.difficulty.strip() or "unknown"
            out.setdefault(key, []).append(r)
        return out

    def section(label: str, w: list[EvalResult], wo: list[EvalResult]) -> list[str]:
        aw, awo = _group_avgs(w), _group_avgs(wo)
        return [
            f"[{label}]",
            (f"  tools:        {aw['tools']:.1f} vs {awo['tools']:.1f}  ({_pct_change(aw['tools'], awo['tools'])})"),
            (
                f"  tokens:       {aw['tokens']:.0f} vs {awo['tokens']:.0f}"
                f"  ({_pct_change(aw['tokens'], awo['tokens'])})"
            ),
            (
                f"  duration:     {aw['duration_ms']:.0f} vs {awo['duration_ms']:.0f} ms"
                f"  ({_pct_change(aw['duration_ms'], awo['duration_ms'])})"
            ),
        ]

    lines: list[str] = [
        "",
        "=" * 60,
        "DATALINK IMPROVEMENT (with vs without; -% = lower cost)",
        "=" * 60,
        "",
    ]
    lines.extend(section("overall", with_rows, without_rows))

    with_by_diff = by_difficulty(with_rows)
    without_by_diff = by_difficulty(without_rows)
    diffs = sorted(set(with_by_diff) | set(without_by_diff), key=_difficulty_sort_key)
    for diff in diffs:
        if diff == "unknown" and len(diffs) == 1:
            continue
        w = with_by_diff.get(diff, [])
        wo = without_by_diff.get(diff, [])
        if not w or not wo:
            continue
        lines.append("")
        lines.extend(section(diff, w, wo))

    return lines


def format_summary(results: list[EvalResult]) -> str:
    """Build the eval summary text (overall + per-difficulty), shared by terminal and .md."""
    by_mode: dict[str, list[EvalResult]] = {}
    for r in results:
        by_mode.setdefault(r.mode, []).append(r)

    lines: list[str] = [
        "=" * 60,
        "EVAL SUMMARY",
        "=" * 60,
    ]

    for mode, rows in sorted(by_mode.items()):
        lines.append("")
        lines.extend(_format_group_stats(mode, rows))

        by_diff: dict[str, list[EvalResult]] = {}
        for r in rows:
            key = r.difficulty.strip() or "unknown"
            by_diff.setdefault(key, []).append(r)

        if len(by_diff) <= 1 and next(iter(by_diff), "unknown") == "unknown":
            continue

        for diff in sorted(by_diff.keys(), key=_difficulty_sort_key):
            lines.append("")
            lines.extend(_format_group_stats(f"{mode} / {diff}", by_diff[diff]))

    with_rows = by_mode.get("with_datalink", [])
    without_rows = by_mode.get("without_datalink", [])
    if with_rows and without_rows:
        lines.extend(_format_datalink_improvement(with_rows, without_rows))

    return "\n".join(lines)


def print_summary(results: list[EvalResult]) -> None:
    print("\n" + format_summary(results))


def write_summary_md(results: list[EvalResult], path: Path) -> None:
    body = format_summary(results)
    path.write_text(f"```\n{body}\n```\n", encoding="utf-8")


def write_json(path: Path, payload: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as out_f:
        json.dump(payload, out_f, ensure_ascii=False, indent=2)
        out_f.write("\n")
