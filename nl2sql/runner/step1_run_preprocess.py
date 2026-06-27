"""Step1 本地预处理入口脚本

作用：
- 顺序执行 Step1 各预处理 builder。
- 支持断点续跑：每步状态记录在 log/preprocess/state/<step_name>.json（平铺布局）。
- 产物落地在 workspace/preprocess/... 供后续步骤复用。

命令行参数：
- --force-rerun：即使状态文件已标记完成，也强制重建所选步骤。
- --verbose / -v：开启 debug 级日志。
- --limit <int>：过滤后最多处理 N 个数据库。
- --db-ids <csv>：逗号分隔的数据库 id，例如 financial,student_club。
- --steps <csv>：逗号分隔的 builder 步骤名；省略时按顺序运行全部 Step1 builder。

已注册步骤名（9 个 builder）：
- step1a_build_schema_cache
- step1b_enhance_column_desc
- step1c_build_column_vectors
- step1d_build_join_graph
- step1e_build_lsh_indexes
- step1f1_extract_value_enum
- step1f2_build_value_desc_vectors
- step1g_build_value_vector_db
- step1h_build_few_shot_examples

执行示例：
- 对单个数据库运行全部步骤：
  python -m nl2sql.runner.step1_run_preprocess --force-rerun --db-ids financial
- 仅运行某一步：
  python -m nl2sql.runner.step1_run_preprocess --force-rerun --steps step1f1_extract_value_enum
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nl2sql import config
from nl2sql.common.atomic_io import atomic_write_json
from nl2sql.preprocess.core.context import Step1BuildContext
from nl2sql.preprocess.builders.registry import build_step_builders


def _parse_db_ids(raw: str | None) -> List[str]:
    if not raw:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _parse_steps(raw: str | None) -> List[str]:
    if not raw:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _configure_logging(log_dir: Path, verbose: bool) -> Path:
    """Configure logging with a timestamped log file (Beijing time). Returns log path."""
    from datetime import datetime, timezone, timedelta
    from nl2sql.common.runner_utils import BeijingFormatter
    bj_tz = timezone(timedelta(hours=8))
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=bj_tz).strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"step1_run_preprocess_{ts}.log"
    formatter = BeijingFormatter("%(asctime)s [%(levelname)s] %(message)s")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[console_handler, file_handler],
        force=True,
    )
    return log_path


def _verify_outputs(outputs: List[str]) -> Dict[str, Any]:
    existing = [path for path in outputs if Path(path).exists()]
    missing = [path for path in outputs if not Path(path).exists()]
    return {"existing": existing, "missing": missing, "ok": len(missing) == 0}


def main() -> None:
    parser = argparse.ArgumentParser(description="Step 1: Local preprocess artifact builder")
    parser.add_argument("--force-rerun", action="store_true", default=False)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--db-ids", type=str, default=None)
    parser.add_argument("--steps", type=str, default=None)
    args = parser.parse_args()

    context = Step1BuildContext.from_args(
        force_rerun=args.force_rerun,
        verbose=args.verbose,
        db_ids=_parse_db_ids(args.db_ids),
        limit=args.limit,
    )

    log_path = _configure_logging(context.artifacts.log_file_path.parent, args.verbose)
    logger = logging.getLogger(__name__)
    logger.info("=== Step1 Local Preprocess Start ===")
    logger.info("BIRD_TABLES_JSON=%s", config.BIRD_TABLES_JSON)
    logger.info("BIRD_DB_DIR=%s", config.BIRD_DB_DIR)
    logger.info("STEP1_CACHE_DIR=%s", config.STEP1_CACHE_DIR)
    logger.info("STEP1_VALUE_VECTOR_STORE_DIR=%s", config.STEP1_VALUE_VECTOR_STORE_DIR)
    logger.info("FEW_SHOT_CACHE_DIR=%s", config.FEW_SHOT_CACHE_DIR)
    logger.info("FEW_SHOT_PATH=%s", config.FEW_SHOT_PATH)

    # 恢复摘要：扫描平铺 state 目录打印各 step 状态
    state_dir = context.artifacts.state_dir
    if state_dir.exists():
        logger.info("--- Step1 resume scan: %s ---", state_dir)
        for state_path in sorted(state_dir.glob("*.json")):
            try:
                import json as _json
                payload = _json.loads(state_path.read_text(encoding="utf-8"))
                logger.info(
                    "  %s: status=%s completed=%d",
                    state_path.stem,
                    payload.get("status", "?"),
                    len(payload.get("completed_keys") or []),
                )
            except Exception as exc:
                logger.warning("  failed to read %s: %s", state_path, exc)

    step_names = _parse_steps(args.steps)
    builders = build_step_builders(context, only_steps=step_names)
    summary: Dict[str, Any] = {
        "status": "success",
        "force_rerun": bool(args.force_rerun),
        "steps": [],
    }

    import json as _json2
    import time as _time

    for builder in builders:
        # 预检查 state 文件判断步骤是否已完成
        _step_state_path = state_dir / f"{builder.step_name}.json"
        _pre_status = ""
        if _step_state_path.exists() and not args.force_rerun:
            try:
                _st = _json2.loads(_step_state_path.read_text(encoding="utf-8"))
                if _st.get("status") == "success":
                    _pre_status = " [SKIP - already completed]"
            except Exception:
                pass
        logger.info("Running %s%s", builder.step_name, _pre_status)
        _t0 = _time.time()
        result = builder.run()
        _elapsed = _time.time() - _t0
        # 执行后状态摘要
        if result.processed_count == 0 and result.skipped_count > 0:
            logger.info("  \u2192 %s [SKIPPED] all %d items already completed (%.1fs)",
                        builder.step_name, result.skipped_count, _elapsed)
        else:
            logger.info("  \u2192 %s [DONE] processed=%d, skipped=%d (%.1fs)",
                        builder.step_name, result.processed_count, result.skipped_count, _elapsed)
        verification = _verify_outputs(result.outputs)
        result_dict = result.to_dict()
        result_dict["verification"] = verification
        summary["steps"].append(result_dict)
        if not verification["ok"]:
            summary["status"] = "failed"
            summary["failed_step"] = builder.step_name
            summary["missing_outputs"] = verification["missing"]
            atomic_write_json(context.artifacts.summary_path, summary)
            raise FileNotFoundError(f"step output verification failed for {builder.step_name}: {verification['missing']}")

    atomic_write_json(context.artifacts.summary_path, summary)
    logger.info("Summary written to %s", context.artifacts.summary_path)
    logger.info("=== Step1 Local Preprocess Complete ===")


if __name__ == "__main__":
    main()
