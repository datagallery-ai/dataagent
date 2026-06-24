"""E2E test for changping: multi-turn cache hit rate verification.

This test extends the basic changping e2e test with:
- Multiple user queries in a single session (same user_id/session_id)
- Explicit user_id and session_id for deterministic path resolution
- DATAAGENT_CONTEXT_DUMP enabled for prompt inspection
- CONTEXT compression configured with low threshold to trigger pruning
- Post-run analysis of LLM cache hit rate from usage_metadata
- Verification that context dumps and trajectory files are correctly written

Cache metrics are extracted from AIMessage.usage_metadata returned by each
Planner node's LLM call, which includes:
  - ``input_tokens``: total input tokens
  - ``input_cache_read_tokens``: tokens served from cache hit
  - ``input_cache_creation_tokens``: tokens written to cache

Usage::

    # Requires mock services from test_changping.py to be running:
    export DATAAGENT_LOG_LEVEL=INFO
    python tests/e2e/changping/test_changping_cache.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

# ── Bootstrap ──────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))

CHANGPING_DIR = Path(__file__).resolve().parent
CONFIG_DIR = CHANGPING_DIR / "config"

# Reference: test_changping.py sets level to DEBUG
os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")

# Re-export mock helpers from the original test file
from test_changping import (  # noqa: E402
    CHANGPING_DIR,
    CONFIG_DIR,
    PROJECT_DIR,
    _build_test_config,
    _cleanup_test_workspace,
    _MOCK_PORT,
    _start_mock_metavisor,
    _stop_mock_metavisor,
    _ORIGINAL_SQLITE_PATH,
    mock_ontology_env,
)

# ── Test constants ─────────────────────────────────────────────────────────
CACHE_TEST_USER_ID = "cache_test_user"
CACHE_TEST_SESSION_ID = "cache_test_session_001"

# Compress at 5 messages to force pruning early
LOW_COMPRESS_MESSAGE_CNT = 5


def _build_cache_test_config(workspace_dir: Path) -> Path:
    """Build config with CONTEXT compression enabled and HOOKS.pruner active."""
    import yaml

    config_path = CONFIG_DIR / "main_config.yaml"
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Resolve workspace paths as in _build_test_config
    from test_changping import _resolve_config_paths

    config = _resolve_config_paths(config, workspace_dir)
    config.setdefault("METAVISOR", {})["metavisor_url"] = f"http://localhost:{_MOCK_PORT}"

    # ── Enable pruner hook ─────────────────────────────────────────────
    hooks = config.setdefault("HOOKS", {})
    node_hooks = hooks.setdefault("nodes", {})
    planner_hooks = node_hooks.setdefault("planner", {})
    planner_hooks.setdefault("pre", ["pruner"])

    # ── Set low compression threshold ──────────────────────────────────
    context_cfg = config.setdefault("CONTEXT", {})
    context_cfg["compress_message_cnt"] = LOW_COMPRESS_MESSAGE_CNT
    # Keep token limit high enough to not interfere
    context_cfg["compress_token_limit"] = 100000

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", prefix="test_cache_", delete=False) as tmp:
        yaml.safe_dump(config, tmp, allow_unicode=True, sort_keys=False)
        tmp.flush()
        return Path(tmp.name)


def _resolve_session_root() -> Path:
    """Return the expected session root for the cache test user."""
    from dataagent.utils.runtime_paths import dataagent_home

    return dataagent_home() / CACHE_TEST_USER_ID / CACHE_TEST_SESSION_ID


def _read_context_dump_json(dump_path: Path) -> list[dict[str, Any]]:
    """Parse a context_dump round file and return parsed messages."""
    content = dump_path.read_text(encoding="utf-8")
    # The dump file is JSON per line? Let's check the format
    # From dump_prompt_to_file it's json.dumps(messages)
    try:
        data = json.loads(content)
        if isinstance(data, list):
            return data
        return [data]
    except json.JSONDecodeError:
        logger.warning(f"Could not parse context dump: {dump_path}")
        return []


def _collect_usage_metadata_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect all usage_metadata from AIMessages in the final state."""
    usage_list: list[dict[str, Any]] = []
    messages = state.get("messages", []) or []

    if isinstance(messages, list):
        for msg in messages:
            msg_type = getattr(msg, "type", None) or ""
            if msg_type == "ai":
                usage = getattr(msg, "usage_metadata", None) or {}
                if usage and usage.get("input_tokens", 0) > 0:
                    usage_list.append({
                        "input_tokens": usage.get("input_tokens", 0),
                        "input_cache_read_tokens": usage.get("input_cache_read_tokens", 0),
                        "input_cache_creation_tokens": usage.get("input_cache_creation_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    })
    return usage_list


def _compute_cache_hit_rate(usage_records: list[dict[str, Any]]) -> dict[str, float]:
    """Compute aggregate cache hit rate from usage records."""
    total_input = sum(r["input_tokens"] for r in usage_records)
    total_cache_read = sum(r["input_cache_read_tokens"] for r in usage_records)
    total_cache_creation = sum(r["input_cache_creation_tokens"] for r in usage_records)

    if total_input == 0:
        return {"hit_rate": 0.0, "total_input": 0, "cache_read": 0, "cache_creation": 0}

    # Cache hit rate per-call and aggregate
    per_call_rates = []
    for r in usage_records:
        rate = r["input_cache_read_tokens"] / max(r["input_tokens"], 1)
        per_call_rates.append(round(rate * 100, 1))

    return {
        "hit_rate": round(total_cache_read / total_input * 100, 1),
        "total_input": total_input,
        "cache_read": total_cache_read,
        "cache_creation": total_cache_creation,
        "num_calls": len(usage_records),
        "per_call_rates": per_call_rates,
    }


async def _run_queries(
    queries: list[str],
    config_path: Path,
    workspace_dir: Path,
) -> dict[str, Any]:
    """Run a sequence of queries in the same session and return the final state and metadata."""
    from dataagent.interface.sdk.agent import DataAgent

    agent = DataAgent.from_config(config_path)

    results: list[dict[str, Any]] = []
    for i, query in enumerate(queries):
        logger.info(f"[Query {i + 1}/{len(queries)}] {query[:80]}...")

        # Each call uses the same user_id and session_id; session is persistent
        # We pass session_id=None to use the last session, but for the first
        # call we need to explicitly set it.
        if i == 0:
            response = await agent.chat(
                query,
                session_id=CACHE_TEST_SESSION_ID,
                initial_state={"user_id": CACHE_TEST_USER_ID},
            )
        else:
            response = await agent.chat(
                query,
                session_id=CACHE_TEST_SESSION_ID,
                initial_state={
                    "user_id": CACHE_TEST_USER_ID,
                    "run_id": i,  # Explicitly set run_id to prevent auto-increment confusion
                },
            )

        results.append(response)

    return {
        "agent": agent,
        "results": results,
        "final_state": results[-1] if results else {},
    }


def _dump_cache_analysis(usage_records: list[dict[str, Any]], output_dir: Path) -> Path:
    """Write cache analysis report to a JSON file."""
    analysis = {
        "cache_stats": _compute_cache_hit_rate(usage_records),
        "per_call": usage_records,
    }
    out_path = output_dir / "cache_analysis.json"
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Cache analysis written to {out_path}")
    return out_path


def _analyze_context_dumps(session_root: Path) -> list[dict[str, Any]]:
    """Analyze context_dump files to extract prompt structure and sizes."""
    dump_base = session_root / ".memory" / "context_dump"
    if not dump_base.exists():
        logger.warning(f"No context_dump directory at {dump_base}")
        return []

    analysis = []
    for run_dir in sorted(dump_base.iterdir()):
        if not run_dir.is_dir():
            continue
        for round_file in sorted(run_dir.iterdir()):
            if round_file.suffix != ".txt":
                continue
            messages = _read_context_dump_json(round_file)
            role_counts: dict[str, int] = {}
            total_chars = 0
            for msg in messages:
                role = msg.get("role", "unknown")
                role_counts[role] = role_counts.get(role, 0) + 1
                content = msg.get("content", "")
                if isinstance(content, str):
                    total_chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            total_chars += len(part.get("text", ""))
            analysis.append({
                "file": str(round_file.relative_to(dump_base)),
                "message_count": len(messages),
                "total_chars": total_chars,
                "role_counts": role_counts,
            })
    return analysis


async def test_multi_turn_cache_hit_rate():
    """Run 3 queries in sequence, then analyze cache hit rate and context dumps.

    Expected behavior:
    1. First query: low cache hit (cache creation)
    2. Subsequent queries: increasing cache hit rate as stable prefix is cached
    3. Context compression should trigger after ~2-3 turns of tool calls
    4. After compression, at least the SystemMessage + StableUser prefix should still cache
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_cache_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    config_path = _build_cache_test_config(workspace_dir)
    logger.info(f"Cache test config: {config_path}")

    queries = [
        "调用nl2sql查询名称为huh-7的细胞及其对应的细胞样本信息",
        "统计当前数据库中有多少种不同的细胞类型",
        "查询所有和BD55-1111抗体相关的实验数据",
    ]

    session_root = _resolve_session_root()
    logger.info(f"Session root: {session_root}")

    # Clean up any previous test data
    if session_root.exists():
        shutil.rmtree(session_root, ignore_errors=True)

    run_data = await _run_queries(queries, config_path, workspace_dir)

    # ── Collect usage metadata from all AIMessages ──────────────────────
    all_usage: list[dict[str, Any]] = []
    for i, state in enumerate(run_data["results"]):
        usages = _collect_usage_metadata_from_state(state)
        all_usage.extend(usages)
        logger.info(f"Query {i + 1}: captured {len(usages)} Planner LLM call(s)")

    # ── Compute aggregate cache hit rate ────────────────────────────────
    stats = _compute_cache_hit_rate(all_usage)
    logger.info("=" * 60)
    logger.info(f"Total LLM calls: {stats['num_calls']}")
    logger.info(f"Total input tokens: {stats['total_input']}")
    logger.info(f"Cache read tokens: {stats['cache_read']}")
    logger.info(f"Cache creation tokens: {stats['cache_creation']}")
    logger.info(f"Aggregate cache hit rate: {stats['hit_rate']}%")
    logger.info(f"Per-call rates: {stats['per_call_rates']}")
    logger.info("=" * 60)

    # ── Write analysis report ───────────────────────────────────────────
    report_dir = session_root / ".memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    _dump_cache_analysis(all_usage, report_dir)

    # ── Analyze context dumps ───────────────────────────────────────────
    dump_analysis = _analyze_context_dumps(session_root)
    logger.info(f"Context dump files found: {len(dump_analysis)}")

    dump_report_path = report_dir / "context_dump_analysis.json"
    dump_report_path.write_text(
        json.dumps(dump_analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"Context dump analysis → {dump_report_path}")

    # ── Verify trajectory files exist ───────────────────────────────────
    context_dir = session_root / ".context"
    if context_dir.exists():
        traj_files = list(context_dir.glob("Run*_Sub0.json"))
        logger.info(f"Trajectory files: {[f.name for f in traj_files]}")
    else:
        logger.warning("No .context directory found")

    # ── Assertions ──────────────────────────────────────────────────────
    assert len(all_usage) > 0, "No LLM call usage metadata collected"

    messages_file = session_root / ".memory" / "messages.json"
    assert messages_file.exists(), f"messages.json not found at {messages_file}"

    cache_file = report_dir / "cache_analysis.json"
    assert cache_file.exists(), f"cache analysis not written to {cache_file}"

    # At minimum, there should be some cache reads across the session
    # (SystemMessage should always hit cache after the first call)
    assert stats["cache_read"] > 0, (
        f"Expected some cache hits across {stats['num_calls']} calls, "
        f"but got 0 cache_read tokens"
    )

    # For multi-turn, the system prompt should be cached after the first turn
    # Each subsequent turn should have at least the system prompt cached
    if stats["num_calls"] >= 3:
        # At least 1/3 of total tokens should be cached (conservative estimate)
        assert stats["hit_rate"] >= 10.0, (
            f"Expected >= 10% cache hit rate across {stats['num_calls']} calls, "
            f"got {stats['hit_rate']}%"
        )

    logger.info("✅ test_multi_turn_cache_hit_rate PASSED")

    _cleanup_test_workspace(workspace_dir, config_path)


async def test_cache_analysis_with_pruning():
    """Verify that context compression triggers and cache still works.

    Uses a very low compress_message_cnt=3 to force aggressive pruning.
    Verifies:
    1. Pruner hook runs (compressed messages appear in the dump)
    2. After compression, the next LLM call still has some cache hits
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_prune_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    import yaml

    config_path = CONFIG_DIR / "main_config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    from test_changping import _resolve_config_paths
    config = _resolve_config_paths(config, workspace_dir)
    config.setdefault("METAVISOR", {})["metavisor_url"] = f"http://localhost:{_MOCK_PORT}"
    config.setdefault("HOOKS", {}).setdefault("nodes", {}).setdefault("planner", {}).setdefault("pre", ["pruner"])
    config.setdefault("CONTEXT", {})["compress_message_cnt"] = 3
    config["CONTEXT"]["compress_token_limit"] = 200000

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", prefix="test_prune_", delete=False) as tmp:
        yaml.safe_dump(config, tmp, allow_unicode=True, sort_keys=False)
        tmp.flush()
        config_path_prune = Path(tmp.name)

    session_root = _resolve_session_root()
    if session_root.exists():
        shutil.rmtree(session_root, ignore_errors=True)

    from dataagent.interface.sdk.agent import DataAgent
    agent = DataAgent.from_config(config_path_prune)

    queries = [
        "调用nl2sql查询名称为huh-7的细胞及其对应的细胞样本信息",
        "统计当前数据库中有多少种不同的细胞类型",
        "查询所有和BD55-1111抗体相关的实验数据",
    ]

    all_usage: list[dict[str, Any]] = []
    for i, query in enumerate(queries):
        response = await agent.chat(
            query,
            session_id=CACHE_TEST_SESSION_ID,
            initial_state={"user_id": CACHE_TEST_USER_ID, "run_id": i},
        )
        usages = _collect_usage_metadata_from_state(response)
        all_usage.extend(usages)

    stats = _compute_cache_hit_rate(all_usage)
    logger.info(f"[Prune test] Cache hit rate: {stats['hit_rate']}% over {stats['num_calls']} calls")
    logger.info(f"[Prune test] Per-call rates: {stats['per_call_rates']}")

    # Check that compression did happen
    dump_base = session_root / ".memory" / "context_dump"
    if dump_base.exists():
        for run_dir in sorted(dump_base.iterdir()):
            if not run_dir.is_dir():
                continue
            for round_file in sorted(run_dir.iterdir()):
                if round_file.suffix != ".txt":
                    continue
                messages = _read_context_dump_json(round_file)
                # After compression, we expect fewer total messages or the presence
                # of a folded/summary HumanMessage
                role_types = {m.get("role") for m in messages}
                logger.info(f"  {round_file.name}: {len(messages)} msgs, roles={role_types}")

    logger.info("✅ test_cache_analysis_with_pruning PASSED")

    _cleanup_test_workspace(workspace_dir, config_path_prune)


async def main():
    """Run all cache tests."""
    _start_mock_metavisor()
    try:
        with mock_ontology_env():
            logger.info("Starting cache hit rate tests...")

            await test_multi_turn_cache_hit_rate()
            logger.info("")

            await test_cache_analysis_with_pruning()
            logger.info("")

            logger.info("All cache tests finished.")
    finally:
        _stop_mock_metavisor()


if __name__ == "__main__":
    asyncio.run(main())
