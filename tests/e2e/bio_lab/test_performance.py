# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""E2E test for bio_lab: main Agent cache hit rate optimization v3.0.

This test verifies the v3.0 cache optimization (D6: move runtime_environment
out of SystemMessage into VariableUser) by replaying the user's real
2026-06-22 session query sequence. The 7-query session is split across two
DataAgent processes to simulate a real session restart:

  process 0: Q1-Q3 (normal continuous conversation)
  process 1: Q4-Q7 (session resumed from disk after restart)

Key fixes verified by this test:
- D6: SystemMessage no longer contains dynamic CPU%/Memory% values, so bp 1
  (System) cache prefix stays byte-stable across process restarts.
- D8 (revised): session_history_restore now loads the full messages.json
  history on restart (max_history_messages folding was removed). Session
  compression is delegated to the pruner hook.
- D2.1: DATAAGENT_CACHE_BREAKPOINT_ANNOTATION=1 enables bp position
  annotation in context_dump for offline inspection.

Design principles (per user request):
- No time-based assertions (queries can take arbitrarily long).
- Do NOT delete intermediate logs, context dumps, trajectories, or workspace
  artifacts — they are preserved for offline analysis.
- Cache hit rate assertions are placed at the END of the test, after all
  queries have completed, so the test runs to completion before any failure.

Usage::

    export DATAAGENT_CACHE_ANCHOR=1
    export DATAAGENT_CONTEXT_DUMP=1
    export DATAAGENT_CACHE_BREAKPOINT_ANNOTATION=1
    python tests/e2e/bio_lab/test_performance.py
    python tests/e2e/bio_lab/test_performance.py --skip_slow
    python tests/e2e/bio_lab/test_performance.py --bad_cases
    python tests/e2e/bio_lab/test_performance.py --bad_cases --query_no 3
    python tests/e2e/bio_lab/test_performance.py --tc_cases --query_no 1
    python tests/e2e/bio_lab/test_performance.py --semantic_layer mock
    python tests/e2e/bio_lab/test_performance.py \
      --semantic_layer semantic_layer \
      --semantic_layer_url http://8.92.9.219:32000 \
      --config main_config_retrieve.yaml

    # Switch model preset (default: deepseek). bailian enables cache_control.
    python tests/e2e/bio_lab/test_performance.py --model bailian

    # Override pruner / IR thresholds.
    python tests/e2e/bio_lab/test_performance.py \\
        --compress_message_cnt 100 --recent_turns 50

    # Re-extract from a prior run.
    python tests/e2e/bio_lab/test_performance.py --tc2_only \\
        --user_id cache_test_user_v3_20260622_141023_ab12 \\
        --session_id cache_test_session_v3_20260622_141023_ab12

CLI options:
  --model {deepseek,openai,bailian}  Model preset for MODEL.chat_model.
                                 openai → openai/Qwen3.7-Plus via OPENAI_BASE_URL.
                                 bailian → bailian/Qwen3.7-Plus via DashScope (cache_control).
                                 deepseek → deepseek/deepseek-v4-flash.
  --compress_message_cnt N       CONTEXT.compress_message_cnt pruner threshold.
  --recent_turns N               CONTEXT.recent_turns IR threshold (0 = replace
                                 all turns).
  --bad_cases                    Run the Changping bad-case query group only.
  --tc_cases                     Run the imported TC query group only.
  --query_no N                   Run only the 1-based query number in the active
                                 group (default replay, --bad_cases, or
                                 --tc_cases group).
  --config PATH                  Agent config YAML under tests/e2e/bio_lab/config
                                 or an absolute/relative file path.

Each run auto-generates a fresh parameter-labeled, timestamped
user_id/session_id under dataagent_home()
(e.g. ``cache_test_user_v3_20260623_141023_ab12_quick__model-deepseek__cfg-main-config__compress-200__recent-200__threshold-optimized``),
so historical artifacts are easy to identify, never collide, and never need
manual archiving.
"""

import asyncio
import json
import os
import random
import secrets
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from loguru import logger
from performance_cache_analysis import (
    _analyze_context_dumps,
    _collect_usage_metadata_from_messages,
    _compute_cache_hit_rate,
    _dump_cache_analysis,
    _extract_cache_from_messages_file,
    _extract_final_assistant_text,
    _format_per_query_summary,
    _verify_system_message_stability,
)
from performance_cli import run_single_query, run_tool_mode
from performance_config import (
    _DEFAULT_AGENT_CONFIG_FILE,
    _ORIGINAL_SQLITE_PATH,
    DEFAULT_COMPRESS_MESSAGE_CNT,
    DEFAULT_MODEL_CHOICE,
    DEFAULT_RECENT_TURNS,
    MODEL_PRESETS,
    _build_cache_test_config,
    _load_agent_config,
    _resolve_agent_config_path,
    _resolve_config_paths,
)
from performance_functional_assertions import verify_functional_correctness
from performance_query_cases import (
    EXPECTED_ANTIBODY_COUNT,
    EXPECTED_ANTIBODY_IDS,
    EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID,
    EXPECTED_BD368_NEUTRALIZED_PSEUDOVIRUSES,
    EXPECTED_CELL_COUNT,
    EXPECTED_CELL_IDS,
    EXPECTED_HUH7_CELL_SAMPLE_ID,
    EXPECTED_NEW_EXPERIMENT_STATUS,
    EXPECTED_PSEUDOVIRUS_COUNT,
    EXPECTED_PSEUDOVIRUS_IDS,
    EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID,
    PROCESS_0_KEYS,
    parse_query_numbers,
    query_sequence_for_group,
    select_query_keys,
)
from performance_run_identity import build_cache_test_ids, build_run_parameter_label
from performance_semantic_mock import (
    _disable_proxy_env,
    _start_mock_metavisor,
    _stop_mock_metavisor,
    configure_semantic_layer,
    get_mock_port,
    get_semantic_layer_mode,
    get_semantic_layer_url,
    set_mock_port,
    uses_mock_semantic_layer,
)
from performance_visualization import generate_cache_visualization

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))

BIO_LAB_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BIO_LAB_DIR / "config"

os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")
os.environ.setdefault("DATAAGENT_CACHE_ANCHOR", "1")
os.environ.setdefault("DATAAGENT_CACHE_BREAKPOINT_ANNOTATION", "1")


# ---------------------------------------------------------------------------
# Automated human feedback with HITL assertion
# ---------------------------------------------------------------------------
_FEEDBACK_RESPONSES: list[str] = []
_FEEDBACK_INDEX = 0
_HITL_TRIGGERED = False


def _auto_input(prompt: str) -> str:
    """Mock input() that returns pre-configured feedback responses and records HITL trigger."""
    global _FEEDBACK_INDEX, _HITL_TRIGGERED
    _HITL_TRIGGERED = True
    if len(_FEEDBACK_RESPONSES) > _FEEDBACK_INDEX:
        response = _FEEDBACK_RESPONSES[_FEEDBACK_INDEX]
        _FEEDBACK_INDEX += 1
        logger.info(f"[Auto HITL] Prompt: {prompt.strip()!r} → Response: {response!r}")
        return response
    logger.warning(f"[Auto HITL] No more configured responses, returning empty string. Prompt: {prompt.strip()!r}")
    return ""


@contextmanager
def auto_human_feedback(responses: list[str]):
    """Patch builtins.input to automatically provide human feedback responses."""
    global _FEEDBACK_INDEX, _HITL_TRIGGERED, _FEEDBACK_RESPONSES
    _FEEDBACK_RESPONSES = responses
    _FEEDBACK_INDEX = 0
    _HITL_TRIGGERED = False
    logger.info(f"[Auto HITL] Configured {len(responses)} feedback response(s): {responses}")

    with patch("builtins.input", _auto_input):
        yield


# Each test run gets a fresh, timestamped user_id / session_id directory under
# dataagent_home(). Generated IDs include a filesystem-safe parameter summary,
# so historical artifacts are easier to identify and never collide or require
# manual archiving between runs. Override via --user_id / --session_id CLI args
# (e.g. for --tc2_only re-extraction on a prior run).
_RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_RUN_SUFFIX = secrets.token_hex(2)  # 4 hex chars, avoids same-second collisions
CACHE_TEST_USER_ID, CACHE_TEST_SESSION_ID = build_cache_test_ids(
    run_stamp=_RUN_STAMP,
    run_suffix=_RUN_SUFFIX,
    parameter_label="default",
)
_CACHE_TEST_USER_ID_EXPLICIT = False
_CACHE_TEST_SESSION_ID_EXPLICIT = False

OVERALL_HIT_RATE_THRESHOLD = 45.0
# Post-creation threshold accounts for Q1 cold-start calls (no history_summary,
# bp1=System only ~2429 tokens (no StableUser) → iteration-first calls rely on bp3 tail_anchor). Q2-Q7
# (which have history_summary → bp1 ~7900-8500 tokens) average 81.5%.
POST_CREATION_HIT_RATE_THRESHOLD = 73.0
RESTART_FIRST_CALL_HIT_RATE_THRESHOLD = 20.0

CACHE_THRESHOLD_PROFILE = "optimized"  # "optimized" | "baseline" | "off"


def _set_generated_cache_test_identity(parameter_label: str) -> None:
    """Refresh generated IDs while preserving explicit CLI overrides."""
    global CACHE_TEST_USER_ID, CACHE_TEST_SESSION_ID

    user_id, session_id = build_cache_test_ids(
        run_stamp=_RUN_STAMP,
        run_suffix=_RUN_SUFFIX,
        parameter_label=parameter_label,
    )
    if not _CACHE_TEST_USER_ID_EXPLICIT:
        CACHE_TEST_USER_ID = user_id
    if not _CACHE_TEST_SESSION_ID_EXPLICIT:
        CACHE_TEST_SESSION_ID = session_id


def _resolve_session_root() -> Path:
    from dataagent.utils.runtime_paths import dataagent_home

    return dataagent_home() / CACHE_TEST_USER_ID / CACHE_TEST_SESSION_ID


def _snapshot_sql_files(workspace_dir: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    if not workspace_dir.exists():
        return snapshot
    for path in workspace_dir.rglob("*.sql"):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(workspace_dir).parts):
            continue
        stat = path.stat()
        snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _collect_changed_sql_files(workspace_dir: Path, before: dict[str, tuple[int, int]]) -> list[str]:
    changed: list[str] = []
    if not workspace_dir.exists():
        return changed
    for path in workspace_dir.rglob("*.sql"):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(workspace_dir).parts):
            continue
        stat = path.stat()
        resolved = str(path.resolve())
        if before.get(resolved) != (stat.st_mtime_ns, stat.st_size):
            changed.append(resolved)
    return sorted(changed)


async def test_v3_session_replay(
    skip_slow: bool = False,
    quick: bool = False,
    query_group: str = "default",
    query_numbers: list[int] | None = None,
    model_choice: str = DEFAULT_MODEL_CHOICE,
    compress_message_cnt: int = DEFAULT_COMPRESS_MESSAGE_CNT,
    recent_turns: int | None = DEFAULT_RECENT_TURNS,
    config_file: str | Path = _DEFAULT_AGENT_CONFIG_FILE,
) -> dict[str, Any]:
    """TC1: Replay the user's real 2026-06-22 session query sequence.

    Two-process session replay that mirrors the real-world scenario of a
    long-running session surviving an agent restart:

      process 0 (no restart):   Q1 create_experiment → Q2 find_antibody_neutralization → Q3 find_recent_experiment
      process 1 (restart):      Q4 count_cells → Q5 count_viruses → Q6 count_antibodies → Q7 ask_recent_experiment_id

    - Process 0: a single DataAgent instance handles Q1-Q3 consecutively.
      In-memory state (messages, plan, cache) carries over between queries.
      bp3 (tail_anchor) should provide high hit rates within this process.

    - Process 1: a fresh DataAgent instance resumes the same session from
      disk (session_history_restore loads the full messages.json history;
      session compression is delegated to the pruner hook).
      Only Q4's first LLM call is a true "restart first call" — its cache
      hit rate measures whether bp1 (System) stays byte-stable
      across process restarts (the D6 metric). Q5-Q7 then run consecutively
      within process 1, relying on bp3 again.

    Artifacts (logs, context dumps, trajectories) are preserved — no cleanup.
    Cache hit rate assertions are deferred to the end of the test.

    Args:
        skip_slow: If True, skip the "create experiment" query (takes ~10 min).
        quick: If True, run only 3 fast count queries in a single process
            (~5 min total, CI smoke test — no restart, no D6 verification).
        query_group: Query group to run: ``"default"``, ``"bad_cases"``, or ``"TC"``.
        query_numbers: Optional 1-based query numbers within the active group.
        model_choice: Model preset for the test config — one of
            ``{"deepseek", "openai", "bailian"}`` (see :data:`MODEL_PRESETS`).
        compress_message_cnt: ``CONTEXT.compress_message_cnt`` threshold for
            the pruner hook (message-count based compression trigger).
        recent_turns: ``CONTEXT.recent_turns`` IR-replacement threshold
            (0 = replace all turns).
        config_file: Agent config YAML to use. Pass ``main_config_retrieve.yaml``
            to test the semantic retrieve ontology tool.

    Returns:
        dict with usage stats, per-query stats, and verification results.
    """

    _set_generated_cache_test_identity(
        build_run_parameter_label(
            model_choice=model_choice,
            config_file=config_file,
            quick=quick,
            skip_slow=skip_slow,
            query_group=query_group,
            query_numbers=query_numbers,
            compress_message_cnt=compress_message_cnt,
            recent_turns=recent_turns,
            cache_threshold_profile=CACHE_THRESHOLD_PROFILE,
        )
    )

    session_root = _resolve_session_root()
    # Each run uses a timestamp-suffixed user_id/session_id, so this directory
    # is fresh by construction. The rmtree below is a defensive guard for the
    # rare case of same-second collisions or an explicit --user_id/--session_id
    # override pointing at a pre-existing dir. Artifacts from THIS run are
    # preserved after the test completes (no automatic cleanup).
    memory_dir = session_root / "workspace" / ".memory"
    if memory_dir.exists():
        shutil.rmtree(memory_dir, ignore_errors=True)
        logger.info(f"TC1 cleaned up pre-existing .memory dir: {memory_dir}")
    session_root.mkdir(parents=True, exist_ok=True)

    workspace_dir = session_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "bio_lab.sqlite")

    config_path = _build_cache_test_config(
        workspace_dir,
        compress_message_cnt=compress_message_cnt,
        enable_human_feedback=True,
        session_root=session_root,
        model_choice=model_choice,
        recent_turns=recent_turns,
        config_file=config_file,
    )
    logger.info(f"TC1 config: {config_path}")
    logger.info(f"TC1 source_config: {_resolve_agent_config_path(config_file)}")
    logger.info(
        f"TC1 model_choice={model_choice}, compress_message_cnt={compress_message_cnt}, recent_turns={recent_turns}"
    )
    logger.info(f"TC1 query_group={query_group}, query_numbers={query_numbers}")
    logger.info(f"TC1 workspace: {workspace_dir} (preserved after test)")

    logger.info(f"TC1 session_root (fresh per run): {session_root}")
    logger.info(f"TC1 user_id={CACHE_TEST_USER_ID}")
    logger.info(f"TC1 session_id={CACHE_TEST_SESSION_ID}")

    query_sequences = query_sequence_for_group(query_group)
    query_keys = select_query_keys(
        query_group=query_group,
        skip_slow=skip_slow,
        quick=quick,
        query_numbers=query_numbers,
    )
    targeted_or_bad_case_run = query_group != "default" or query_numbers is not None
    logger.info(f"TC1 query sequence ({len(query_keys)} queries): {query_keys}")

    all_usage: list[dict[str, Any]] = []
    per_query_usages: list[dict[str, Any]] = []
    per_query_responses: list[dict[str, Any]] = []

    # Two-process replay: reuse DataAgent instances within each process.
    # process_agents[0] serves Q1-Q3, process_agents[1] serves Q4-Q7.
    # Only the first query of process 1 is a true restart (session resumed
    # from disk); within-process queries reuse in-memory state.
    process_agents: dict[int, Any] = {}
    # Per-process snapshot of len(response["messages"]) after each chat() call.
    # Within a process the DataAgent instance is reused, so response["messages"]
    # accumulates across queries — slicing by this offset isolates the AIMessage
    # usage_metadata produced by the current query only (fix §1.5 per-query
    # cumulative stats bug).
    prev_msgs_len_by_proc: dict[int, int] = {}
    restart_query_indices: list[int] = []  # execution-order indices of restart queries

    for i, query_key in enumerate(query_keys):
        spec = query_sequences[query_key]
        query = spec["query"]
        logger.info("=" * 60)
        logger.info(f"[TC1 Query {i + 1}/{len(query_keys)}] key={query_key}")
        logger.info(f"  query={query!r}")
        logger.info(f"  needs_feedback={spec.get('needs_feedback', False)}")

        # Determine process index: quick mode always uses process 0 (no
        # restart); full mode splits at PROCESS_0_KEYS / PROCESS_1_KEYS
        # boundary so Q1-Q3 share one DataAgent, Q4-Q7 share another.
        process_idx = 0 if (quick or targeted_or_bad_case_run) else 0 if query_key in PROCESS_0_KEYS else 1

        # Create a new DataAgent instance only at process boundaries.
        # Within a process, the same instance is reused so in-memory
        # state (messages, plan, cache) carries over between queries.
        if process_idx not in process_agents:
            from dataagent.interface.sdk.agent import DataAgent

            process_agents[process_idx] = DataAgent.from_config(config_path)
            if process_idx > 0:
                # Only process 1+ is a true "restart" — it resumes an existing
                # session from disk. Process 0 is a cold start (new session,
                # no prior cache), so it is NOT counted as a restart.
                restart_query_indices.append(i)
                # session_history_restore loads prior queries' messages from
                # messages.json into state["messages"] inside chat(). Those
                # restored messages carry STALE usage_metadata (from their
                # original generation in a prior process). To avoid
                # double-counting them in per-query stats, initialize
                # prev_msgs_len to the persisted message count so the slice
                # only captures NEW messages produced by this chat() call.
                _messages_file = session_root / "workspace" / ".memory" / "messages.json"
                if _messages_file.exists():
                    try:
                        from dataagent.core.context.message_history import read_messages_file

                        _restored_count = len(read_messages_file(_messages_file))
                        prev_msgs_len_by_proc[process_idx] = _restored_count
                        logger.info(
                            f"  [Process {process_idx}] Session resumed from disk (restart); "
                            f"{_restored_count} restored messages excluded from per-query usage"
                        )
                    except Exception as e:
                        logger.warning(f"  [Process {process_idx}] Failed to read messages.json baseline: {e}")
                        logger.info(f"  [Process {process_idx}] Session resumed from disk (restart)")
                else:
                    logger.info(f"  [Process {process_idx}] Session resumed from disk (restart)")
            else:
                logger.info(f"  [Process {process_idx}] New DataAgent instance created")

        agent = process_agents[process_idx]

        initial_state = {
            "user_id": CACHE_TEST_USER_ID,
            "run_id": i,
        }
        # max_history_messages folding was removed; session_history_restore
        # now loads the full messages.json history on restart. Within-process
        # queries carry in-memory state, so restore is a no-op for them.

        # Snapshot the message count BEFORE chat() so we can slice off only
        # the messages produced by this chat() call (the agent instance is
        # reused within a process, so response["messages"] is cumulative).
        pre_msgs_len = prev_msgs_len_by_proc.get(process_idx, 0)

        # Time the end-to-end execution of this single user question
        # (from agent.chat() entry to its return). Covers Planner loop,
        # tool execution, and any human-feedback round-trips.
        sql_files_before_query = _snapshot_sql_files(workspace_dir)
        t_start = time.perf_counter()
        if spec.get("needs_feedback") and spec.get("feedback_responses"):
            with auto_human_feedback(spec["feedback_responses"]):
                response = await agent.chat(
                    query,
                    session_id=CACHE_TEST_SESSION_ID,
                    initial_state=initial_state,
                )
        else:
            response = await agent.chat(
                query,
                session_id=CACHE_TEST_SESSION_ID,
                initial_state=initial_state,
            )
        hitl_triggered = _HITL_TRIGGERED if spec.get("needs_feedback") else False
        t_end = time.perf_counter()
        elapsed_sec = round(t_end - t_start, 2)
        generated_sql_files = _collect_changed_sql_files(workspace_dir, sql_files_before_query)

        all_msgs = response.get("messages", []) or []
        # History should never shrink between calls within a process; if it
        # does (unexpected), fall back to the full list rather than silently
        # dropping usage data.
        new_msgs = all_msgs[pre_msgs_len:] if pre_msgs_len <= len(all_msgs) else all_msgs
        usages = _collect_usage_metadata_from_messages(new_msgs)
        if all_msgs or pre_msgs_len == 0:
            prev_msgs_len_by_proc[process_idx] = len(all_msgs)
        else:
            logger.warning(
                f"  Agent returned no messages for query '{query_key}'; preserving previous "
                f"message baseline ({pre_msgs_len}) for the next query"
            )
        all_usage.extend(usages)
        per_query_usages.append(
            {
                "query_key": query_key,
                "query": query,
                "usages": usages,
                "elapsed_sec": elapsed_sec,
                "process_idx": process_idx,
                "is_restart": i in restart_query_indices,
            }
        )

        # Capture summary without storing the full state (could be huge)
        msgs = response.get("messages", []) or []
        final_answer = _extract_final_assistant_text(new_msgs)
        per_query_responses.append(
            {
                "query_key": query_key,
                "num_messages": len(msgs),
                "num_llm_calls": len(usages),
                "elapsed_sec": elapsed_sec,
                "final_answer": final_answer,
                "hitl_triggered": hitl_triggered,
                "sql_files": generated_sql_files,
            }
        )
        logger.info(
            f"  Captured {len(usages)} Planner LLM calls, {len(msgs)} messages "
            f"in {elapsed_sec}s (process={process_idx})"
        )
        if generated_sql_files:
            logger.info(f"  Generated/updated SQL files: {generated_sql_files}")
        if final_answer:
            preview = final_answer[:200].replace("\n", " ")
            logger.info(f"  Final answer preview: {preview}{'...' if len(final_answer) > 200 else ''}")
        else:
            logger.warning(f"  No final assistant text captured for query '{query_key}'")

    # ------------------------------------------------------------------
    # Aggregate statistics
    # ------------------------------------------------------------------
    overall_stats = _compute_cache_hit_rate(all_usage)
    post_creation_usage = [u for u in all_usage if u["input_cache_read_tokens"] > 0]
    post_creation_stats = (
        _compute_cache_hit_rate(post_creation_usage) if post_creation_usage else {"hit_rate": 0.0, "num_calls": 0}
    )

    logger.info("=" * 60)
    logger.info("TC1 Results:")
    logger.info(f"  Total LLM calls: {overall_stats['num_calls']}")
    logger.info(f"  Total input tokens: {overall_stats['total_input']}")
    logger.info(f"  Total output tokens: {overall_stats['total_output']}")
    logger.info(f"  Cache read tokens: {overall_stats['cache_read']}")
    logger.info(f"  Cache creation tokens: {overall_stats['cache_creation']}")
    logger.info(f"  Overall hit rate: {overall_stats['hit_rate']}%")
    logger.info(
        f"  Post-creation hit rate: {post_creation_stats['hit_rate']}% ({post_creation_stats['num_calls']} calls)"
    )
    logger.info(f"  Per-call rates: {overall_stats['per_call_rates']}")
    logger.info("  Per-query breakdown:")
    for i, q in enumerate(per_query_usages, 1):
        q_stats = _compute_cache_hit_rate(q["usages"])
        restart_tag = " [RESTART]" if q.get("is_restart") else ""
        logger.info(
            f"  [{i}/{len(per_query_usages)}] Query '{q['query_key']}'{restart_tag}: "
            f"calls={q_stats['num_calls']}, hit_rate={q_stats['hit_rate']}%, "
            f"input={q_stats['total_input']}, output={q_stats['total_output']}, "
            f"cache_read={q_stats['cache_read']}, cache_creation={q_stats['cache_creation']}, "
            f"elapsed={q.get('elapsed_sec', 0)}s, "
            f"per_call={q_stats['per_call_rates']}"
        )
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Verify D6 fix: SystemMessage stability across restarts
    # ------------------------------------------------------------------
    system_check = _verify_system_message_stability(session_root)
    logger.info(f"D6 verification: stable={system_check['stable']}")
    logger.info(f"  reason: {system_check['reason']}")
    for s in system_check.get("system_samples", [])[:5]:
        logger.info(
            f"  {s['file']}: chars={s['system_chars']}, has_cpu={s['has_cpu_line']}, has_memory={s['has_memory_line']}"
        )

    # ------------------------------------------------------------------
    # Restart-first-call hit rate (key D6 metric)
    # ------------------------------------------------------------------
    # Only the first LLM call of each RESTART query (first query of a new
    # process) is a "restart first call". With D6 fix, bp1 (System)
    # should be byte-stable → cache hit on restart first call.
    # Within-process queries (Q2, Q3, Q5-Q7) are NOT restarts — their first
    # calls rely on bp3 (tail_anchor) carried over from the previous query
    # in the same process.
    restart_first_calls: list[dict[str, Any]] = []
    for q in per_query_usages:
        if q.get("is_restart") and q["usages"]:
            restart_first_calls.append(q["usages"][0])
    restart_first_stats = _compute_cache_hit_rate(restart_first_calls)
    logger.info(
        f"Restart-first-call hit rate: {restart_first_stats['hit_rate']}% (n={restart_first_stats['num_calls']})"
    )
    logger.info(f"  Per-call rates: {restart_first_stats['per_call_rates']}")

    # ------------------------------------------------------------------
    # Within-process hit rate (supplementary metric)
    # ------------------------------------------------------------------
    # All calls from non-restart queries (Q2-Q3 in process 0, Q5-Q7 in
    # process 1). These rely on bp3 (tail_anchor) for high hit rates.
    # The restart query's first call is excluded (it's a cold resume).
    within_process_usage = [u for q in per_query_usages if not q.get("is_restart") for u in q["usages"]]
    within_process_stats = (
        _compute_cache_hit_rate(within_process_usage) if within_process_usage else {"hit_rate": 0.0, "num_calls": 0}
    )
    logger.info(f"Within-process hit rate: {within_process_stats['hit_rate']}% (n={within_process_stats['num_calls']})")

    # ------------------------------------------------------------------
    # Persist analysis reports (no deletion of artifacts)
    # ------------------------------------------------------------------
    report_dir = session_root / "workspace" / ".memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    _dump_cache_analysis(all_usage, report_dir, label="v3_overall")
    _dump_cache_analysis(post_creation_usage, report_dir, label="v3_post_creation")
    _dump_cache_analysis(restart_first_calls, report_dir, label="v3_restart_first_call")
    _dump_cache_analysis(within_process_usage, report_dir, label="v3_within_process")

    dump_analysis = _analyze_context_dumps(session_root)
    dump_report_path = report_dir / "context_dump_analysis_v3.json"
    dump_report_path.write_text(json.dumps(dump_analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Context dump files analyzed: {len(dump_analysis)}")

    # Save system stability verification
    sys_report_path = report_dir / "system_stability_v3.json"
    sys_report_path.write_text(json.dumps(system_check, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"System stability report: {sys_report_path}")

    # Save per-query breakdown
    per_query_report = {
        "queries": [
            {
                "query_key": q["query_key"],
                "query": q["query"],
                "num_calls": len(q["usages"]),
                "elapsed_sec": q.get("elapsed_sec", 0),
                "process_idx": q.get("process_idx", 0),
                "is_restart": q.get("is_restart", False),
                "stats": _compute_cache_hit_rate(q["usages"]),
            }
            for q in per_query_usages
        ],
    }
    per_query_report_path = report_dir / "per_query_stats_v3.json"
    per_query_report_path.write_text(json.dumps(per_query_report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # DEFERRED assertions (placed at end, after all queries complete)
    # ------------------------------------------------------------------
    # Pre-compute a per-query summary string used in assertion messages so that
    # a failing assertion shows BOTH per-LLM-call rates AND per-question totals
    # (hit_rate, input/output/cache tokens). This makes cache regressions much
    # easier to localise to a specific query.
    per_query_summary = _format_per_query_summary(per_query_usages)

    assert len(all_usage) > 0, (
        f"No LLM call usage metadata collected across {len(query_keys)} queries\n{per_query_summary}"
    )

    messages_file = session_root / "workspace" / ".memory" / "messages.json"
    assert messages_file.exists(), f"messages.json not found at {messages_file}"

    assert overall_stats["cache_read"] > 0, (
        f"Expected some cache hits across {overall_stats['num_calls']} calls, "
        f"but got 0 cache_read tokens. Per-call: {overall_stats['per_call_rates']}\n"
        f"{per_query_summary}"
    )

    if CACHE_THRESHOLD_PROFILE == "optimized":
        assert overall_stats["hit_rate"] >= OVERALL_HIT_RATE_THRESHOLD, (
            f"Overall cache hit rate {overall_stats['hit_rate']}% < {OVERALL_HIT_RATE_THRESHOLD}%. "
            f"Per-call: {overall_stats['per_call_rates']}\n"
            f"{per_query_summary}"
        )

        assert post_creation_stats["hit_rate"] >= POST_CREATION_HIT_RATE_THRESHOLD, (
            f"Post-creation cache hit rate {post_creation_stats['hit_rate']}% < "
            f"{POST_CREATION_HIT_RATE_THRESHOLD}%. "
            f"Per-call (post-creation, {post_creation_stats['num_calls']} calls): "
            f"{post_creation_stats['per_call_rates']}\n"
            f"{per_query_summary}"
        )
    elif CACHE_THRESHOLD_PROFILE == "baseline":
        logger.info(
            f"Baseline profile: overall hit rate {overall_stats['hit_rate']}% "
            f"(threshold {OVERALL_HIT_RATE_THRESHOLD}% not enforced)"
        )
        logger.info(
            f"Baseline profile: post-creation hit rate {post_creation_stats['hit_rate']}% "
            f"(threshold {POST_CREATION_HIT_RATE_THRESHOLD}% not enforced)"
        )
    else:
        logger.info(f"Threshold profile is '{CACHE_THRESHOLD_PROFILE}': all cache hit rate assertions are disabled")

    # D6 verification: SystemMessage must NOT contain CPU/Memory lines
    assert system_check["stable"], (
        f"D6 fix verification failed: SystemMessage still contains CPU/Memory lines. "
        f"Samples: {system_check['system_samples'][:3]}"
    )

    # Restart-first-call hit rate: key metric for D6.
    # Only the first call of each RESTART query (process boundary) is
    # measured. Without D6, restart's first call had read=0 (cache rebuild).
    # With D6, bp1 should be byte-stable → first call hits the cache.
    if CACHE_THRESHOLD_PROFILE == "optimized":
        assert restart_first_stats["hit_rate"] >= RESTART_FIRST_CALL_HIT_RATE_THRESHOLD, (
            f"Restart-first-call hit rate {restart_first_stats['hit_rate']}% < "
            f"{RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}%. This is the key D6 metric — "
            f"if it fails, SystemMessage is still varying across process restarts. "
            f"Restart queries: {restart_query_indices}. "
            f"Per-call (first call of each restart query, {restart_first_stats['num_calls']} calls): "
            f"{restart_first_stats['per_call_rates']}\n"
            f"{per_query_summary}"
        )
    elif CACHE_THRESHOLD_PROFILE == "baseline":
        logger.info(
            f"Baseline profile: restart-first-call hit rate {restart_first_stats['hit_rate']}% "
            f"(threshold {RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}% not enforced)"
        )

    # Compression safety: bp1 (history_summary) must NOT miss after compression.
    # The only call with cache_read=0 should be Q1's first call (cold start,
    # no prior cache). If any Q2-Q7 call has cache_read=0, it means bp1 missed
    # — likely because compression broke history_summary (a regression in
    # _find_head_count or pruner logic).
    zero_read_calls = [
        {"query": q["query_key"], "call_idx": j, "input": u["input_tokens"]}
        for q in per_query_usages
        for j, u in enumerate(q["usages"])
        if u["input_cache_read_tokens"] == 0
    ]
    # Q1 (first query, cold start) is allowed one zero-read call (its first call).
    # Any additional zero-read calls indicate bp1 miss after compression.
    max_allowed_zero_reads = 1 if len(per_query_usages) > 0 else 0
    assert len(zero_read_calls) <= max_allowed_zero_reads, (
        f"Found {len(zero_read_calls)} calls with cache_read=0 (expected at most "
        f"{max_allowed_zero_reads} for Q1 cold start). This means bp1 "
        f"(history_summary) missed — compression may have broken it. "
        f"Zero-read calls: {zero_read_calls}\n"
        f"{per_query_summary}"
    )
    if zero_read_calls:
        logger.info(
            f"Compression safety: {len(zero_read_calls)} zero-read call(s) "
            f"(expected: Q1 cold start only) — bp1 survived compression ✅"
        )

    functional_results, created_experiment_id = verify_functional_correctness(
        per_query_responses,
        workspace_dir,
        report_dir,
    )

    logger.info("TC1 PASSED:")
    logger.info("TC1 PASSED:")
    if CACHE_THRESHOLD_PROFILE == "optimized":
        logger.info(f"  Overall: {overall_stats['hit_rate']}% >= {OVERALL_HIT_RATE_THRESHOLD}%")
        logger.info(f"  Post-creation: {post_creation_stats['hit_rate']}% >= {POST_CREATION_HIT_RATE_THRESHOLD}%")
        logger.info(
            f"  Restart-first-call: {restart_first_stats['hit_rate']}% >= {RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}%"
        )
    else:
        logger.info(f"  Overall: {overall_stats['hit_rate']}% (threshold: {OVERALL_HIT_RATE_THRESHOLD}%, not enforced)")
        logger.info(
            f"  Post-creation: {post_creation_stats['hit_rate']}% (threshold: {POST_CREATION_HIT_RATE_THRESHOLD}%, not enforced)"
        )
        logger.info(
            f"  Restart-first-call: {restart_first_stats['hit_rate']}% (threshold: {RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}%, not enforced)"
        )
    logger.info(f"  Within-process: {within_process_stats['hit_rate']}% (n={within_process_stats['num_calls']})")
    logger.info(f"  D6 system stability: {system_check['stable']}")
    logger.info(
        f"  Functional checks: {len(functional_results)} queries verified"
        f"{f' (created_experiment_id={created_experiment_id})' if created_experiment_id else ''}"
    )
    logger.info(f"  Workspace preserved at: {workspace_dir}")
    logger.info(f"  Session root preserved at: {session_root}")
    logger.info("=" * 60)

    return {
        "overall_stats": overall_stats,
        "post_creation_stats": post_creation_stats,
        "restart_first_stats": restart_first_stats,
        "within_process_stats": within_process_stats,
        "system_check": system_check,
        "per_query_usages": per_query_usages,
        "functional_results": functional_results,
        "created_experiment_id": created_experiment_id,
        "workspace_dir": workspace_dir,
        "session_root": session_root,
    }


async def test_v3_offline_extraction(session_root: Path) -> dict[str, Any]:
    """TC2: Offline extraction from messages.json (no in-process state).

    Verifies that the messages.json file contains valid usage_metadata and
    the cache hit rate can be recomputed offline from the persisted file.

    Args:
        session_root: Path to the session root (from TC1).

    Returns:
        dict with offline extraction stats.
    """
    offline_stats = _extract_cache_from_messages_file(session_root)
    logger.info(f"TC2 Offline extraction: hit_rate={offline_stats['hit_rate']}%, calls={offline_stats['num_calls']}")

    messages_file = session_root / "workspace" / ".memory" / "messages.json"
    assert messages_file.exists(), f"messages.json should exist at {messages_file}"

    assert offline_stats["num_calls"] > 0, f"Expected > 0 LLM calls in messages.json, got {offline_stats['num_calls']}"

    assert offline_stats["cache_read"] > 0, (
        f"Expected cache_read > 0 from messages.json extraction, got {offline_stats['cache_read']}"
    )

    logger.info(f"TC2 PASSED: offline extraction works, hit_rate={offline_stats['hit_rate']}%")
    return {"offline_stats": offline_stats}


async def main():
    import argparse

    global CACHE_THRESHOLD_PROFILE

    _disable_proxy_env()

    parser = argparse.ArgumentParser(description="Bio_lab cache v3.0 e2e test / interactive tool")
    parser.add_argument(
        "--tool_mode",
        action="store_true",
        help="Interactive mode: start mock services and enter free terminal chat",
    )
    parser.add_argument(
        "--user",
        "-u",
        default=None,
        metavar="USER_ID",
        help="用户 ID（默认 anonymous，由 run_terminal_mode 兜底）",
    )
    parser.add_argument(
        "--session",
        "-s",
        default=None,
        metavar="SESSION_ID",
        help="会话 ID：默认本进程内生成 时间戳_uuid；指定则固定该会话 ID",
    )
    parser.add_argument("--skip_slow", action="store_true", help="Skip the slow 'create experiment' query")
    parser.add_argument("--quick", action="store_true", help="Run only 3 fast count queries (~5 min, CI smoke test)")
    parser.add_argument("--bad_cases", action="store_true", help="Run only the Changping bad-case query group")
    parser.add_argument("--tc_cases", action="store_true", help="Run only the imported TC query group")
    parser.add_argument(
        "--query_no",
        default=None,
        metavar="N[,N...]",
        help="Run only the 1-based query number(s) in the active group, e.g. --query_no 3 or --query_no 1,4.",
    )
    parser.add_argument(
        "--config",
        default=_DEFAULT_AGENT_CONFIG_FILE,
        metavar="YAML",
        help=(
            "Agent config YAML to use. Relative names are resolved under tests/e2e/bio_lab/config. "
            "Use main_config_retrieve.yaml with --semantic_layer semantic_layer --semantic_layer_url for the "
            "semantic retrieve ontology tool."
        ),
    )
    parser.add_argument("--tc2_only", action="store_true", help="Run only TC2 (offline extraction) on existing session")
    parser.add_argument(
        "--user_id",
        default=None,
        help="Override CACHE_TEST_USER_ID (mainly for --tc2_only re-extraction on a prior run). "
        "Default: timestamp-suffixed fresh ID per run.",
    )
    parser.add_argument(
        "--session_id",
        default=None,
        help="Override CACHE_TEST_SESSION_ID (mainly for --tc2_only re-extraction on a prior run). "
        "Default: timestamp-suffixed fresh ID per run.",
    )
    parser.add_argument(
        "--query",
        "-q",
        default=None,
        metavar="QUESTION",
        help="单次问答模式：直接传入问题，启动 mock 服务并获取回答后退出",
    )
    parser.add_argument(
        "--viz_only",
        action="store_true",
        help="Only generate the HTML visualization from existing context_dump (no test run).",
    )
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_PRESETS),
        default=DEFAULT_MODEL_CHOICE,
        help=f"Model preset for MODEL.chat_model (default: {DEFAULT_MODEL_CHOICE}). "
        f"openai → openai/Qwen3.7-Plus; bailian → bailian/qwen3.7-plus (DashScope, cache_control); "
        "deepseek → deepseek-v4-flash.",
    )
    parser.add_argument(
        "--compress_message_cnt",
        type=int,
        default=DEFAULT_COMPRESS_MESSAGE_CNT,
        help=f"CONTEXT.compress_message_cnt threshold for pruner hook (default: {DEFAULT_COMPRESS_MESSAGE_CNT}).",
    )
    parser.add_argument(
        "--recent_turns",
        type=int,
        default=DEFAULT_RECENT_TURNS,
        help=f"CONTEXT.recent_turns IR-replacement threshold (default: {DEFAULT_RECENT_TURNS}). "
        "0 means replace all turns (distance >= 0 is always true).",
    )
    parser.add_argument(
        "--mock_port",
        type=int,
        default=None,
        help="MetaVisor mock server port. Default: random in [32000, 32999].",
    )
    parser.add_argument(
        "--semantic_layer",
        choices=["mock", "semantic_layer"],
        default=None,
        help=(
            "Semantic-layer source for ontology and NL2SQL metadata. "
            "mock uses the inline fixture server; semantic_layer uses --semantic_layer_url."
        ),
    )
    parser.add_argument(
        "--semantic_layer_url",
        default=None,
        metavar="URL",
        help="Semantic-layer base URL required when --semantic_layer semantic_layer is selected.",
    )
    parser.add_argument(
        "--cache-threshold-profile",
        choices=["optimized", "baseline", "off"],
        default=CACHE_THRESHOLD_PROFILE,
        help=f"Cache hit rate assertion profile (default: {CACHE_THRESHOLD_PROFILE}). "
        "optimized → enforce strict thresholds (CI gate); "
        "baseline → log but skip assertions (measurement); "
        "off → disable all hit rate assertions.",
    )
    args = parser.parse_args()

    global CACHE_TEST_USER_ID, CACHE_TEST_SESSION_ID
    global _CACHE_TEST_SESSION_ID_EXPLICIT, _CACHE_TEST_USER_ID_EXPLICIT
    if args.mock_port:
        set_mock_port(args.mock_port)
    elif get_mock_port() == 0:
        set_mock_port(random.randint(32000, 32999))
    semantic_layer_mode = args.semantic_layer
    if semantic_layer_mode is None:
        semantic_layer_mode = (
            "semantic_layer" if args.semantic_layer_url or os.getenv("SEMANTIC_SERVICE_URL") else "mock"
        )
    try:
        configure_semantic_layer(semantic_layer_mode, args.semantic_layer_url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.user_id:
        CACHE_TEST_USER_ID = args.user_id
        _CACHE_TEST_USER_ID_EXPLICIT = True
    if args.session_id:
        CACHE_TEST_SESSION_ID = args.session_id
        _CACHE_TEST_SESSION_ID_EXPLICIT = True
    CACHE_THRESHOLD_PROFILE = args.cache_threshold_profile
    if args.bad_cases and args.tc_cases:
        raise ValueError("--bad_cases and --tc_cases are mutually exclusive")
    query_group = "TC" if args.tc_cases else "bad_cases" if args.bad_cases else "default"
    query_numbers = parse_query_numbers(args.query_no)
    _set_generated_cache_test_identity(
        build_run_parameter_label(
            model_choice=args.model,
            config_file=args.config,
            quick=args.quick,
            skip_slow=args.skip_slow,
            query_group=query_group,
            query_numbers=query_numbers,
            compress_message_cnt=args.compress_message_cnt,
            recent_turns=args.recent_turns,
            cache_threshold_profile=CACHE_THRESHOLD_PROFILE,
            tc2_only=args.tc2_only,
            viz_only=args.viz_only,
            tool_mode=args.tool_mode,
            query=args.query,
            semantic_layer_mode=get_semantic_layer_mode(),
            semantic_layer_url=get_semantic_layer_url(),
        )
    )

    logger.info("=" * 60)
    logger.info("Bio_lab cache v3.0 e2e test starting")
    logger.info(f"  user_id   : {CACHE_TEST_USER_ID}")
    logger.info(f"  session_id : {CACHE_TEST_SESSION_ID}")
    logger.info(f"  session_root: {_resolve_session_root()}")
    logger.info(f"  model     : {args.model}")
    logger.info(f"  compress_message_cnt: {args.compress_message_cnt}")
    logger.info(f"  recent_turns: {args.recent_turns}")
    logger.info(f"  threshold_profile: {CACHE_THRESHOLD_PROFILE}")
    logger.info(f"  config    : {_resolve_agent_config_path(args.config)}")
    logger.info(f"  semantic_layer: {get_semantic_layer_mode()}")
    if get_semantic_layer_url():
        logger.info(f"  semantic_layer_url: {get_semantic_layer_url()}")
    if uses_mock_semantic_layer():
        logger.info(f"  mock_port : {get_mock_port()}")
    logger.info(f"  query_group: {query_group}")
    logger.info(f"  query_no  : {args.query_no}")
    logger.info("=" * 60)

    if args.viz_only:
        session_root = _resolve_session_root()
        html_path = generate_cache_visualization(session_root)
        logger.info(f"Visualization generated: {html_path}")
        return

    if uses_mock_semantic_layer():
        _start_mock_metavisor()
    try:
        if args.tool_mode:
            await run_tool_mode(user_id=args.user, session_id=args.session, config_file=args.config)
            return
        if args.query:
            await run_single_query(args.query, config_file=args.config)
            return

        logger.info("Starting main Agent cache v3.0 tests...")

        if args.tc2_only:
            session_root = _resolve_session_root()
            await test_v3_offline_extraction(session_root)
        else:
            tc1_result = await test_v3_session_replay(
                skip_slow=args.skip_slow,
                quick=args.quick,
                query_group=query_group,
                query_numbers=query_numbers,
                model_choice=args.model,
                compress_message_cnt=args.compress_message_cnt,
                recent_turns=args.recent_turns,
                config_file=args.config,
            )
            logger.info("")
            await test_v3_offline_extraction(tc1_result["session_root"])

        logger.info("All cache v3.0 tests finished.")

        session_root = _resolve_session_root()
        html_path = generate_cache_visualization(session_root)
        logger.info(f"Cache visualization: {html_path}")
    finally:
        _stop_mock_metavisor()


if __name__ == "__main__":

    def _signal_handler(sig, frame):
        _stop_mock_metavisor()
        os._exit(0)

    import signal

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    asyncio.run(main())
