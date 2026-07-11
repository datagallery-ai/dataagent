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
"""Functional guard test for changping HITL sentinel behavior.

This test verifies that when the XBB.1.5 pseudovirus sample (id=904036) is
**missing** from ``wet_samples`` (simulating the data inconsistency from
issue #5), the HITL sentinel mechanism (issue #6) correctly prevents the
planner from autonomously selecting an alternative pseudovirus and creating
the experiment without the user's explicit choice.

**Test scenario:**
1. Copy the source SQLite DB (which has the wet_samples row for 904036).
2. **Delete** the wet_samples row for id=904036 — this causes INNER JOIN
   with wet_samples to drop the XBB.1.5 sample, making it appear "not found".
3. Run the create_experiment query with 1 auto HITL response
   ("确认，请创建该中和实验" — an invalid answer that doesn't pick a
   specific pseudovirus).
4. Assert:
   - The experiment was **NOT** created in the DB (HITL sentinel blocked
     autonomous creation).
   - The sentinel text ("[SYSTEM] 用户连续 N 次未提供有效反馈") appears
     in the conversation messages.
   - The final answer does **NOT** claim the experiment was successfully
     created (the planner truthfully reports XBB.1.5 was not found).

This test complements ``test_performance.py`` (performance/cache-focused),
which asserts the experiment **IS** created when the data is consistent.
Together they guard both the happy path and the HITL-blocked path.

Usage::

    # Run as a script (like test_performance.py)
    uv run tests/e2e/changping/test_changping_functional.py --model openai

    # Run via pytest
    uv run pytest tests/e2e/changping/test_changping_functional.py -v
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

# Ensure project root is on sys.path (same pattern as test_performance.py)
PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.append(str(PROJECT_DIR))

import os  # noqa: E402

# Override mock port before importing changping_common (which reads _MOCK_PORT
# at import time). Using 32001 allows concurrent execution with
# test_performance.py (which uses 32000).
import test_performance as _tp  # noqa: E402

_tp._MOCK_PORT = 32002

from changping_common import (  # noqa: E402
    EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID,
    EXPECTED_HUH7_CELL_SAMPLE_ID,
    EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID,
    MODEL_PRESETS,
    ORIGINAL_SQLITE_PATH,
    auto_human_feedback,
    build_cache_test_config,
    check_experiment_exists,
    check_sentinel_in_messages,
    delete_pseudovirus_sample,
    delete_wet_samples_row,
    disable_proxy_env,
    extract_final_assistant_text,
    start_mock_metavisor,
    stop_mock_metavisor,
)

os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")

# Register atexit so the mock server is cleaned up even on ungraceful exits
# (prevents TIME_WAIT "Address already in use" on subsequent runs).
import atexit  # noqa: E402

atexit.register(stop_mock_metavisor)

# The sample id whose wet_samples row we delete to trigger the HITL scenario.
DELETED_WET_SAMPLES_ID = 904036

# The create_experiment query (same as test_performance.py's QUERY_SEQUENCES)
CREATE_EXPERIMENT_QUERY = "帮我创建BD55-1111抗体和XBB.1.5病毒的中和实验（使用huh-7细胞）"

# Auto HITL responses: 1 invalid answer that doesn't pick a specific pseudovirus.
# After this is consumed, subsequent HITL calls get empty string → sentinel.
AUTO_FEEDBACK_RESPONSES = ["确认，请创建该中和实验"]


@pytest.mark.asyncio
async def test_hitl_sentinel_blocks_experiment_creation() -> dict[str, Any]:
    """Guard: when XBB.1.5 sample is missing from wet_samples, HITL sentinel
    must prevent autonomous experiment creation.

    Returns a dict with test results for assertion / reporting.
    """
    disable_proxy_env()
    start_mock_metavisor()
    try:
        return await _run_hitl_scenario()
    finally:
        stop_mock_metavisor()


async def _run_hitl_scenario() -> dict[str, Any]:
    """Inner scenario body; assumes mock server is already started."""
    # --- Setup workspace ---
    import secrets
    import tempfile

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_suffix = secrets.token_hex(2)
    user_id = f"functional_test_{run_stamp}_{run_suffix}"
    session_id = f"functional_test_{run_stamp}_{run_suffix}"

    from dataagent.utils.runtime_paths import dataagent_home

    session_root = dataagent_home() / user_id / session_id
    session_root.mkdir(parents=True, exist_ok=True)

    workspace_dir = session_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Copy the source SQLite DB (which now HAS the wet_samples row for 904036)
    db_path = workspace_dir / "changping02.sqlite"
    shutil.copy2(ORIGINAL_SQLITE_PATH, db_path)

    # --- Delete the XBB.1.5 sample to trigger HITL "which pseudovirus?" ---
    # Delete from BOTH pseudovirus_samples AND wet_samples to guarantee
    # that no SQL path (with or without wet_samples JOIN) can find 904036.
    # Deleting from pseudovirus_samples alone would suffice, but we also
    # delete from wet_samples to keep the DB consistent (no orphan row).
    deleted_pv = delete_pseudovirus_sample(db_path, DELETED_WET_SAMPLES_ID)
    deleted_ws = delete_wet_samples_row(db_path, DELETED_WET_SAMPLES_ID)
    assert deleted_pv == 1, (
        f"Setup failed: expected to delete 1 pseudovirus_samples row for id={DELETED_WET_SAMPLES_ID}, "
        f"got {deleted_pv}. The source DB may have been modified."
    )
    logger.info(
        f"Deleted pseudovirus_samples row (deleted_pv={deleted_pv}, deleted_ws={deleted_ws}) "
        f"for id={DELETED_WET_SAMPLES_ID} — XBB.1.5 now has zero samples"
    )

    # --- Build config ---
    model_choice = os.environ.get("FUNCTIONAL_TEST_MODEL", "openai")
    config_path = build_cache_test_config(
        workspace_dir,
        enable_human_feedback=True,
        session_root=session_root,
        model_choice=model_choice,
    )
    logger.info(f"Config: {config_path}")
    logger.info(f"Model: {model_choice}")

    # --- Run the agent ---
    from dataagent.interface.sdk.agent import DataAgent

    agent = DataAgent.from_config(str(config_path))
    logger.info(f"Query: {CREATE_EXPERIMENT_QUERY!r}")
    logger.info(f"Auto HITL responses: {AUTO_FEEDBACK_RESPONSES}")

    with auto_human_feedback(AUTO_FEEDBACK_RESPONSES):
        response = await agent.chat(
            CREATE_EXPERIMENT_QUERY,
            session_id=session_id,
        )

    messages = response.get("messages", []) or []
    final_answer = extract_final_assistant_text(messages)

    # --- Assertions ---
    results: dict[str, Any] = {
        "session_root": str(session_root),
        "db_path": str(db_path),
        "final_answer_preview": final_answer[:500],
        "num_messages": len(messages),
    }

    # Assertion 1: Experiment was NOT created in the DB.
    # The HITL sentinel should have blocked the planner from autonomously
    # selecting an alternative pseudovirus and creating the experiment.
    exp_id = check_experiment_exists(
        db_path,
        cell_id=EXPECTED_HUH7_CELL_SAMPLE_ID,
        inhibitor_id=EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID,
        pseudovirus_id=EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID,
    )
    results["experiment_created"] = exp_id is not None
    results["experiment_id"] = exp_id

    assert exp_id is None, (
        f"HITL sentinel FAILED to block experiment creation: "
        f"experiment id={exp_id} was created in the DB with "
        f"cell={EXPECTED_HUH7_CELL_SAMPLE_ID}, inhibitor={EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID}, "
        f"pseudovirus={EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID}. "
        f"This means the planner autonomously selected a pseudovirus and created "
        f"the experiment despite the user not providing a valid choice — "
        f"the HITL sentinel (issue #6) did not work. "
        f"Final answer was:\n{final_answer[:1000]}"
    )
    logger.info("✅ Assertion 1 passed: experiment was NOT created (HITL sentinel blocked)")

    # Assertion 2: Sentinel text appears in messages.
    # The HITL node should have retried 3 times on empty feedback and then
    # injected the sentinel "[SYSTEM] 用户连续 N 次未提供有效反馈..."
    sentinel_info = check_sentinel_in_messages(messages)
    results["sentinel_triggered"] = sentinel_info["sentinel_triggered"]
    results["sentinel_count"] = sentinel_info["sentinel_count"]

    assert sentinel_info["sentinel_triggered"], (
        f"HITL sentinel was NOT triggered: expected at least one ToolMessage "
        f"containing '[SYSTEM]' and '未提供有效反馈', but found none in "
        f"{len(messages)} messages. This means the HITL node did not detect "
        f"empty feedback and inject the sentinel (issue #6 fix not working). "
        f"Final answer was:\n{final_answer[:1000]}"
    )
    logger.info(f"✅ Assertion 2 passed: sentinel triggered ({sentinel_info['sentinel_count']} time(s))")

    # Assertion 3: Final answer does NOT claim experiment was created.
    # The planner should truthfully report that XBB.1.5 was not found and
    # the experiment could not be created.
    creation_claim_markers = [
        "实验已成功创建",
        "实验已创建",
        "experiment id",
        "实验ID",
        "实验编号",
        "902036",  # the expected experiment id (should NOT appear since not created)
    ]
    false_claims = [marker for marker in creation_claim_markers if marker.lower() in final_answer.lower()]
    results["false_creation_claims"] = false_claims

    assert not false_claims, (
        f"Final answer falsely claims the experiment was created: "
        f"found markers {false_claims} in the answer, but the experiment "
        f"was NOT created in the DB. The planner 'forgot' the HITL sentinel "
        f"and falsely reported success (issue #6 sentinel text revision not working). "
        f"Final answer was:\n{final_answer[:1000]}"
    )
    logger.info("✅ Assertion 3 passed: final answer does not falsely claim creation")

    logger.info("=" * 60)
    logger.info("ALL ASSERTIONS PASSED — HITL sentinel behavior is correct")
    logger.info(f"  Experiment created: {results['experiment_created']}")
    logger.info(f"  Sentinel triggered: {results['sentinel_triggered']} ({results['sentinel_count']}x)")
    logger.info(f"  False claims: {results['false_creation_claims']}")
    logger.info(f"  Final answer preview: {final_answer[:200]}")
    logger.info(f"  Session root: {session_root}")
    logger.info("=" * 60)

    return results


# ---------------------------------------------------------------------------
# CLI entry point (run as a script, like test_performance.py)
# ---------------------------------------------------------------------------


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Changping functional guard test: HITL sentinel behavior")
    parser.add_argument(
        "--model",
        choices=list(MODEL_PRESETS.keys()),
        default="openai",
        help="Model preset (default: openai)",
    )
    args = parser.parse_args()

    os.environ["FUNCTIONAL_TEST_MODEL"] = args.model

    logger.info("=" * 60)
    logger.info("Changping functional guard test starting")
    logger.info(f"  Model: {args.model}")
    logger.info(
        f"  Scenario: delete wet_samples id={DELETED_WET_SAMPLES_ID}, verify HITL sentinel blocks experiment creation"
    )
    logger.info("=" * 60)

    await test_hitl_sentinel_blocks_experiment_creation()
    logger.info("Functional guard test PASSED")


if __name__ == "__main__":
    asyncio.run(main())
