"""Step4a 入口脚本：将 Step3b 输出对接到 SFT 选择器

执行示例：
  python -m nl2sql.runner.step4a_sft_runner --run-id <run_id>
"""

from __future__ import annotations

import argparse
import logging
import sys

from .. import config
from ..common.runner_utils import setup_logging
from ..sft_selector.pipeline import parse_qid_list, run_all_questions


logger = logging.getLogger("step4a_sft")
LOG_DIR = config.LOG_DIR / "sft_selector"
MAX_RETRIES = 3


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description="Step4a: SFT selector runner")
    parser.add_argument("--run-id", default=config.SFT_SELECTOR_RUN_ID)
    parser.add_argument("--source-run-id", default=None, help="Step3b run id; default equals --run-id")
    parser.add_argument("--model-path", default=config.SFT_SELECTOR_MODEL_PATH)
    parser.add_argument("--output-name", default=config.SFT_SELECTOR_OUTPUT_NAME)
    parser.add_argument("--force-rerun", action="store_true", default=config.DEFAULT_FORCE_RERUN,
                        help="全量重跑：忽略已存在的 sft_selected 输出，对所有题重新跑。与 --rerun-qids 互斥。")
    parser.add_argument("--rerun-qids", type=str, default=None, metavar="QIDS",
                        help="单题重跑：仅重跑指定题号（逗号/空格/分号分隔），例如 --rerun-qids \"42,108\"。优先级高于 --force-rerun。")
    parser.add_argument("--finalize-only", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    try:
        rerun_qids_set = parse_qid_list(args.rerun_qids)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    if rerun_qids_set and args.force_rerun:
        print(
            "[WARN] --rerun-qids 与 --force-rerun 同时指定，将仅使用 --rerun-qids （单题优先）",
            file=sys.stderr, flush=True,
        )

    source_run_id = args.source_run_id or args.run_id
    log_file = setup_logging(LOG_DIR, args.run_id, "step4a_sft", verbose=args.verbose)
    logger.info(
        "Step4a start: run_id=%s source_run_id=%s output=%s log=%s",
        args.run_id,
        source_run_id,
        args.output_name,
        log_file,
    )

    summary = run_all_questions(
        run_id=args.run_id,
        source_run_id=source_run_id,
        model_path=args.model_path,
        output_name=args.output_name,
        max_retries=MAX_RETRIES,
        force_rerun=args.force_rerun,
        finalize_only=args.finalize_only,
        rerun_qids=rerun_qids_set,
    )

    if summary.get("errors", 0) > 0 or summary.get("missing", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
