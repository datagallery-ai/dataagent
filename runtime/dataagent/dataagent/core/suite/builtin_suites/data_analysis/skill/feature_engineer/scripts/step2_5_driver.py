#!/usr/bin/env python3
"""Driver for step2_5 (finalize): semantic-tail → validate-contract → receipt → verify-all.

Hides the fixed artifact filenames behind one command so the Agent never hand-fills
``--contract`` / ``--schema`` / ``--wide-csv`` / ``--expanded-sql`` paths. Sub-commands:

  * ``validate-contract`` -- ``step2_2_validate_deployment_contract.py`` with defaults
  * ``receipt``           -- ``step2_5_receipt.py``
  * ``verify-all``        -- ``step2_verify_all.py --require-receipt``
  * ``finalize``          -- run all four; collect exit codes; report failures at the end

``finalize`` also auto-chains the step2_3b tail: when ``progress_step2_3b.txt`` exists,
the CSV is exported, and ``semantic_apply.json`` has no gate section yet, it runs
``spearman-gate`` → ``merge-contract`` before validate-contract (idempotent — already
done steps are skipped). This keeps the post-export tail out of the Agent's hands: no
ordering decisions, no skipped steps on resume.

``--cleaning-report`` / ``--report-json`` are forwarded only when the caller passes them
explicitly (no auto-attach from on-disk defaults).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from step2_driver_common import run_script  # noqa: E402

CONTRACT = "step2_2_deployment_feature_contract.json"
SCHEMA = "schema_resolution.json"
WIDE_CSV = "step2_4_wide_userfiltered.csv"
EXPANDED_SQL = "step2_2_feature_aggregation_expanded.sql"
CLEANING_REPORT = "step2_3_cleaning_report.json"
SEMANTIC_MARKER = "progress_step2_3b.txt"
SEMANTIC_APPLY = "semantic_apply.json"


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _run(script: str, argv: list[str]) -> int:
    return run_script(SCRIPTS_DIR / script, argv)


def _validate_contract(args: argparse.Namespace) -> int:
    out_dir: Path = args.out_dir
    contract = args.contract or out_dir / CONTRACT
    schema = args.schema or out_dir / SCHEMA
    wide_csv = args.wide_csv or out_dir / WIDE_CSV
    expanded_sql = args.expanded_sql or out_dir / EXPANDED_SQL

    argv = [
        "--contract", str(contract),
        "--schema", str(schema),
        "--wide-csv", str(wide_csv),
        "--expanded-sql", str(expanded_sql),
    ]
    # Only forward when the user explicitly passed --cleaning-report.
    if args.cleaning_report is not None:
        argv += ["--cleaning-report", str(args.cleaning_report)]
    if args.enforce:
        argv.append("--enforce")
    return _run("step2_2_validate_deployment_contract.py", argv)


def _receipt(args: argparse.Namespace) -> int:
    argv = ["--out-dir", str(args.out_dir)]
    if args.summary is not None:
        argv += ["--summary", str(args.summary)]
    return _run("step2_5_receipt.py", argv)


def _verify_all(args: argparse.Namespace) -> int:
    argv = [
        "--out-dir", str(args.out_dir),
        "--require-receipt",
    ]
    # Only forward when the user explicitly passed --report-json.
    if args.report_json is not None:
        argv += ["--report-json", str(args.report_json)]
    return _run("step2_verify_all.py", argv)


def _semantic_tail(args: argparse.Namespace) -> int:
    """Auto-chain the step2_3b tail (spearman-gate → merge-contract) before validate-contract.

    No-op unless semantic features are enabled (marker present) and the CSV is exported.
    Missing gate section → run spearman-gate; unmerged contract → run merge-contract.
    Already-done steps are skipped, so a finalize re-run (e.g. after a step retry) never
    re-does or double-applies the tail.
    """
    out_dir: Path = args.out_dir
    if not (out_dir / SEMANTIC_MARKER).is_file():
        return 0
    # Import the module directly (registered in sys.modules), not via run_script's
    # importlib load: py3.9 dataclasses resolves string annotations through
    # sys.modules and crashes on unregistered modules.
    import step2_3b_semantic_features as semantic

    csv_path = out_dir / WIDE_CSV
    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        print(
            "step2_5 finalize：检测到 progress_step2_3b.txt 但 step2_4 CSV 尚未导出，"
            "语义尾巴链暂不执行（verify-all 会指出缺失项）"
        )
        return 0
    apply_payload = _read_json(out_dir / SEMANTIC_APPLY) or {}
    ran: list[str] = []
    if not apply_payload.get("gate"):
        code = semantic.run_spearman_gate(out_dir, csv_path=csv_path)
        if code != 0:
            return code
        ran.append("spearman-gate")
    contract_path = args.contract or out_dir / CONTRACT
    contract_payload = _read_json(contract_path) or {}
    if not contract_payload.get("semantic_features"):
        code = semantic.run_merge_contract(out_dir, contract=contract_path)
        if code != 0:
            return code
        ran.append("merge-contract")
    if ran:
        print(f"step2_5 finalize：已自动完成 step2_3b 尾巴链（{' → '.join(ran)}）")
    else:
        print("step2_5 finalize：step2_3b 尾巴链已完成（gate 段与契约合并均在），无需重跑")
    return 0


def _finalize(args: argparse.Namespace) -> int:
    """Run semantic-tail → validate-contract → receipt → verify-all; never skip later steps on failure."""
    steps: list[tuple[str, Callable[[], int]]] = [
        ("semantic-tail", lambda: _semantic_tail(args)),
        ("validate-contract", lambda: _validate_contract(args)),
        ("receipt", lambda: _receipt(args)),
        ("verify-all", lambda: _verify_all(args)),
    ]
    results: list[tuple[str, int]] = []
    print(
        "step2_5 finalize：将依次执行 semantic-tail（step2_3b 生效时自动串联 spearman-gate → "
        "merge-contract）→ validate-contract → receipt → verify-all"
    )
    for index, (name, func) in enumerate(steps, start=1):
        print(f"==> [{index}/4] step2_5 {name}")
        code = int(func() or 0)
        results.append((name, code))
        if code != 0:
            print(f"step2_5 {name} 失败（exit={code}），继续后续步骤", file=sys.stderr)

    failed = [(name, code) for name, code in results if code != 0]
    if failed:
        detail = ", ".join(f"{name}={code}" for name, code in failed)
        print(f"step2_5 finalize 失败：{detail}", file=sys.stderr)
        print(
            "补救指引：\n"
            f"  - validate-contract --enforce 报缺 --cleaning-report 时，重跑：\n"
            f"      python skill/feature-engineer/scripts/step2_5_driver.py finalize "
            f"--out-dir {args.out_dir} --cleaning-report {CLEANING_REPORT} --enforce\n"
            "  - semantic-tail 失败时按 spearman-gate / merge-contract 的报错修正"
            "（通常是 CSV 未含语义列——确认 step2_4 以 step2_3b_wide_semantic 为输入）后重跑 finalize；\n"
            "  - receipt / verify-all 失败时先确认 step2_4 CSV 与 gate marker 齐全，"
            "再单独重跑对应子命令（verify-all / receipt）。",
            file=sys.stderr,
        )
        return 1
    print("step2_5 finalize 全部通过")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="step2_5 driver (finalize)")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-contract", help="run deployment contract structural check")
    validate.add_argument("--out-dir", type=Path, default=Path("."))
    validate.add_argument("--contract", type=Path, default=None)
    validate.add_argument("--schema", type=Path, default=None)
    validate.add_argument("--wide-csv", type=Path, default=None)
    validate.add_argument("--expanded-sql", type=Path, default=None)
    validate.add_argument("--cleaning-report", type=Path, default=None)
    validate.add_argument("--enforce", action="store_true")
    validate.set_defaults(func=_validate_contract)

    receipt = sub.add_parser("receipt", help="generate receipt.json")
    receipt.add_argument("--out-dir", type=Path, default=Path("."))
    receipt.add_argument("--summary", type=str, default=None)
    receipt.set_defaults(func=_receipt)

    verify = sub.add_parser("verify-all", help="run step2_verify_all.py --require-receipt")
    verify.add_argument("--out-dir", type=Path, default=Path("."))
    verify.add_argument("--report-json", type=Path, default=None)
    verify.set_defaults(func=_verify_all)

    finalize = sub.add_parser(
        "finalize",
        help="semantic-tail (auto) → validate-contract → receipt → verify-all (all run; failures summarized)",
    )
    finalize.add_argument("--out-dir", type=Path, default=Path("."))
    finalize.add_argument("--contract", type=Path, default=None)
    finalize.add_argument("--schema", type=Path, default=None)
    finalize.add_argument("--wide-csv", type=Path, default=None)
    finalize.add_argument("--expanded-sql", type=Path, default=None)
    finalize.add_argument("--cleaning-report", type=Path, default=None)
    finalize.add_argument("--enforce", action="store_true")
    finalize.add_argument("--summary", type=str, default=None)
    finalize.add_argument("--report-json", type=Path, default=None)
    finalize.set_defaults(func=_finalize)

    args = parser.parse_args()
    raise SystemExit(args.func(args) or 0)


if __name__ == "__main__":
    main()
