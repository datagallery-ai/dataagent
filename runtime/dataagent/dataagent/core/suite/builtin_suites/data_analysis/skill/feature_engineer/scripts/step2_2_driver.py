#!/usr/bin/env python3
"""Driver for step2_2 (1:N aggregation + list expansion + dimension join).

Hides the multi-pass shape behind a single chain of commands and folds the whole chain
into ``step2_2_run.json``, so the Agent never picks the next command, hand-writes a CTE,
chooses an aggregation, or binds a SQL placeholder. Entry point is ``prepare``; after
that, every stage writes ``after_collect`` pointing at exactly one next command:

  * ``prepare``     -- pass 1: expand + bind term-frequency SQL (possibly batched).
    Next: ``sample`` (after ``check-collect --expect-rows``).
  * ``sample``      -- pass 3: expand ``step2_2_string_sample_expanded.sql``. Next:
    ``string-plan`` when candidates exist, otherwise straight to ``finalize``.
  * ``string-plan`` -- sampled payload -> ``string_cast_plan.txt``. Next: ``finalize``
    with ``--string-plan``.
  * ``finalize``    -- pass 2: expand + bind aggregation / combined validation SQL plus
    plan / contract / derivation. ``after_collect`` points at ``gate``.
  * ``gate``        -- ``step2_2_verify_gate.py`` (2a~2l). On success clears ``after_collect``
    so the chain terminates (do not re-run gate).

Agent flow: run ``prepare``, then for each manifest: submit ``jobs`` in order (if any),
then run ``after_collect``. Keep going until ``after_collect`` is absent. After
``finalize``, submit the CREATE + combined validation collect **before** running ``gate``.

The expansion algorithm lives in ``step2_2_expand_sql.py`` and reads every threshold from
``references/aggregation_policy.json``; the driver only orchestrates and binds the
warehouse catalog placeholder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from step2_2_expand_sql import (  # noqa: E402
    _assert_expanded,
    _load_policy,
    _non_comment_text,
    _read_json,
    _read_list_fields,
    _read_string_plan,
    _rows,
    build_string_sample_query,
    build_term_queries,
    combine_validation_sql,
    derive_string_plan,
    expand,
    split_validation_sql,
    string_cast_candidates,
)
from step2_driver_common import (  # noqa: E402
    bind_placeholders,
    load_data_source,
    run_script,
    unbound_placeholders,
    write_manifest,
)

MANIFEST = "step2_2_run.json"
TERM_SQL = "step2_2_term_query_expanded.sql"
AGGREGATION_SQL = "step2_2_feature_aggregation_expanded.sql"
VALIDATION_SQL = "step2_2_validation_expanded.sql"
COMBINED_VALIDATION_SQL = "step2_2_validation_combined.sql"
PLAN = "step2_2_feature_plan.json"
CONTRACT = "step2_2_deployment_feature_contract.json"
DERIVATION = "step2_2_feature_derivation.md"
RAW_TERMS = "raw_terms.json"
TERM_PAYLOADS_LIST = "term_payloads.txt"
RAW_WIDE_STATS = "raw_wide_stats.json"
STRING_SAMPLE_SQL = "step2_2_string_sample_expanded.sql"
RAW_STRING_SAMPLE = "raw_string_sample.json"
STRING_PLAN = "string_cast_plan.txt"
STRING_CANDIDATES = "step2_2_string_candidates.json"

CHECK_COLLECT = (
    "python skill/feature-engineer/scripts/step2_driver_common.py "
    "check-collect --manifest {manifest}{expect} --out-dir {out_dir}"
)

SAVE_NOTE_HARD_RULES = (
    "落盘指引："
    "① ≤32KB 小结果：把 collect 响应写临时文件（如 tmp_terms_<i>.json）后 "
    "save-collect --source <tmp> --out raw_terms_<i>.json --extract-rows；"
    "也可对已落盘临时文件用 save-collect --batch；"
    "② >32KB 大结果：save-collect --source tool-results/<id>.txt 或 --latest；"
    "③ 给可读文件就能落盘；守卫报缺失时只补 save-collect，勿重 submit。"
)

TERM_IDEMPOTENT_NOTE = (
    "词项幂等：已存在且非空的 raw_terms_*.json（带 .via-save-collect）视为该 job 已完成，"
    "禁止对对应 step2_2_term_query_*.sql 重复 submit/poll/collect；"
    "提交前 ls raw_terms_*.json 或 check-collect --manifest step2_2_run.json --expect-rows；"
    "仅缺失项才提交；缺文件时只补 save-collect，不重提交；"
    "同一 job 落盘失败重交>2 次必须换方法（tool-results 直落 / 标准临时 json），禁止死循环。"
)


def _collect_guard(next_cmd: str, out_dir: Path | str, *, expect_rows: bool = False) -> str:
    """Prefix ``next_cmd`` with a ``check-collect`` guard (``&&`` chain)."""
    expect = " --expect-rows" if expect_rows else ""
    return (
        CHECK_COLLECT.format(manifest=MANIFEST, expect=expect, out_dir=out_dir)
        + " && "
        + next_cmd
    )


def _gate_command(args: argparse.Namespace) -> str:
    return (
        "python skill/feature-engineer/scripts/step2_2_driver.py gate "
        f"--schema {args.schema} --plan {PLAN} "
        f"--sql {AGGREGATION_SQL} --validation {VALIDATION_SQL}"
    )


def _sample_command(args: argparse.Namespace) -> str:
    return (
        "python skill/feature-engineer/scripts/step2_2_driver.py sample "
        f"--schema {args.schema} --meta {args.meta} --dump {args.dump} "
        f"--list-fields {args.list_fields} --out-dir {args.out_dir}"
    )


def _string_plan_command(args: argparse.Namespace) -> str:
    return (
        "python skill/feature-engineer/scripts/step2_2_driver.py string-plan "
        f"--schema {args.schema} --meta {args.meta} --dump {args.dump} "
        f"--list-fields {args.list_fields} --raw-sample {RAW_STRING_SAMPLE} "
        f"--out-dir {args.out_dir}"
    )


def _write_term_payloads(out_dir: Path, names: list[str]) -> None:
    (out_dir / TERM_PAYLOADS_LIST).write_text(
        "\n".join(names) + ("\n" if names else ""), encoding="utf-8"
    )


def _read_term_payloads(out_dir: Path) -> list[str]:
    path = out_dir / TERM_PAYLOADS_LIST
    if path.is_file():
        names = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if names:
            return names
    # Legacy workspace fallback: single raw_terms.json
    if (out_dir / RAW_TERMS).is_file():
        return [RAW_TERMS]
    return [RAW_TERMS]


def _finalize_command(args: argparse.Namespace, string_plan: str | None = None) -> str:
    out_dir = Path(args.out_dir)
    payloads = _read_term_payloads(out_dir)
    cmd = (
        "python skill/feature-engineer/scripts/step2_2_driver.py finalize "
        f"--schema {args.schema} --meta {args.meta} --dump {args.dump} "
        f"--list-fields {args.list_fields} --out-dir {args.out_dir}"
    )
    for name in payloads:
        cmd += f" --raw-terms {name}"
    if string_plan:
        cmd += f" --string-plan {string_plan}"
    return cmd


def _write_sha256(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = Path(str(path) + ".sha256")
    # GNU sha256sum -c format: "<digest>  <filename>\n" (two spaces)
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return sidecar


def _warn_unbound(path: str, text: str) -> None:
    leftovers = unbound_placeholders(text)
    if leftovers:
        print(
            f"警告：{path} 仍有未绑定占位符 {leftovers}；"
            "工作区缺 data_source.json，已回退套件 references/data_source.json；"
            "仍有未绑定占位符，提交前需按 references/data_source.md 手工替换。",
            file=sys.stderr,
        )


def _write_manifest(out_dir: Path, args: argparse.Namespace, stage: str, jobs: list[dict], extra: dict) -> None:
    manifest = {
        "step": "step2_2",
        "stage": stage,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jobs": jobs,
        # Bare gate command; check-collect guard lives only on after_collect.
        "gate": _gate_command(args),
    }
    manifest.update(extra)
    write_manifest(out_dir / MANIFEST, manifest)


def _clear_after_collect(out_dir: Path) -> None:
    """Drop ``after_collect`` so the Agent stops following the chain after a successful gate."""
    path = out_dir / MANIFEST
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or "after_collect" not in payload:
        return
    payload.pop("after_collect", None)
    payload["stage"] = "gate"
    payload["generated_at"] = datetime.now(timezone.utc).isoformat()
    write_manifest(path, payload)


def _prepare(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = _read_json(args.schema)
    dump = _read_json(args.dump)
    list_fields = _read_list_fields(args.list_fields)
    policy = _load_policy()
    data_source = load_data_source(out_dir)

    queries = build_term_queries(schema, dump, list_fields, policy)
    jobs: list[dict] = []
    payloads: list[str] = []
    multi = len(queries) > 1
    for index, term_sql in enumerate(queries):  # 0-based batch names
        term_sql = bind_placeholders(term_sql, data_source)
        if multi:
            command_file = f"step2_2_term_query_{index}.sql"
            collect_as = f"raw_terms_{index}.json"
            job_name = f"step2_2_term_query_{index}"
        else:
            command_file = TERM_SQL
            collect_as = RAW_TERMS
            job_name = "step2_2_term_query"
        out_term = out_dir / command_file
        _assert_expanded(out_term, term_sql)
        out_term.write_text(_non_comment_text(term_sql).rstrip() + "\n", encoding="utf-8")
        print(f"已写出 {out_term}")
        jobs.append({"name": job_name, "command_file": command_file, "collect_as": collect_as})
        payloads.append(collect_as)

    _write_term_payloads(out_dir, payloads)
    # Larger batches cut agent round-trips; keep under MCP payload pressure.
    batch_size = min(8, len(jobs)) or 1
    batch_groups: list[list[str]] = []
    if batch_size > 0:
        for start in range(0, len(jobs), batch_size):
            batch_groups.append([job["name"] for job in jobs[start : start + batch_size]])

    done = [
        job["collect_as"]
        for job in jobs
        if job.get("collect_as")
        and (out_dir / str(job["collect_as"])).is_file()
        and (out_dir / str(job["collect_as"])).stat().st_size > 0
        and (out_dir / f"{job['collect_as']}.via-save-collect").is_file()
    ]
    pending = [job["name"] for job in jobs if job.get("collect_as") not in done]

    _write_manifest(
        out_dir,
        args,
        "prepare",
        jobs,
        {
            "list_fields": list_fields,
            "term_payloads": payloads,
            "batch_size": batch_size,
            "batch_groups": batch_groups,
            "term_jobs_done": done,
            "term_jobs_pending": pending,
            "submit_rule": "command_file_only_skip_done",
            "collect_rule": "batch_then_save",
            "save_note": (
                SAVE_NOTE_HARD_RULES
                + TERM_IDEMPOTENT_NOTE
                + f"当前已落盘 {len(done)}/{len(payloads)}；pending={pending}；"
                "每批仅 submit pending，立即 save-collect --extract-rows；"
                "after_collect 前置 check-collect --expect-rows：全部 raw_terms 非空才放行 sample。"
            ),
            "after_collect": _collect_guard(
                _sample_command(args), out_dir, expect_rows=True
            ),
        },
    )


def _finalize(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = _read_json(args.schema)
    meta = _read_json(args.meta)
    dump = _read_json(args.dump)
    list_fields = _read_list_fields(args.list_fields)
    policy = _load_policy()
    string_plan = _read_string_plan(args.string_plan)

    raw_term_paths = list(args.raw_terms)
    if not raw_term_paths:
        raw_term_paths = [out_dir / name for name in _read_term_payloads(out_dir)]
    for path in raw_term_paths:
        if not path.is_file():
            raise SystemExit(f"未找到词项频次 payload 文件：{path}")
    term_records = _rows(list(raw_term_paths))

    result = expand(schema, meta, dump, list_fields, term_records, string_plan, policy)
    data_source = load_data_source(out_dir)

    aggregation_sql = bind_placeholders(result.aggregation_sql, data_source)
    validation_sql = bind_placeholders(result.validation_sql, data_source)

    out_aggregation = out_dir / AGGREGATION_SQL
    _assert_expanded(out_aggregation, aggregation_sql)
    out_aggregation.write_text(
        _non_comment_text(aggregation_sql).rstrip() + "\n", encoding="utf-8"
    )
    print(f"已写出 {out_aggregation}")
    sidecar = _write_sha256(out_aggregation)
    print(f"已写出 {sidecar}")

    out_validation = out_dir / VALIDATION_SQL
    _assert_expanded(out_validation, validation_sql)
    out_validation.write_text(
        _non_comment_text(validation_sql).rstrip() + "\n", encoding="utf-8"
    )
    print(f"已写出 {out_validation}")
    _warn_unbound(VALIDATION_SQL, validation_sql)

    parts = split_validation_sql(validation_sql)
    validation_files: list[str] = []
    for index, part in enumerate(parts, start=1):
        split_name = f"step2_2_validation_{index}.sql"
        split_path = out_dir / split_name
        split_path.write_text(_non_comment_text(part).rstrip() + "\n", encoding="utf-8")
        print(f"已写出 {split_path}")
        validation_files.append(split_name)

    combined = combine_validation_sql(parts)
    out_combined = out_dir / COMBINED_VALIDATION_SQL
    _assert_expanded(out_combined, combined)
    out_combined.write_text(_non_comment_text(combined).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_combined}")

    out_plan = out_dir / PLAN
    out_plan.write_text(json.dumps(result.plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"已写出 {out_plan}：1:N 表 {len(result.plan['aggregations'])} 张，"
        f"EXCEPT {len(result.plan['except_columns'])} 列，维度表 {len(result.plan['dimension_tables'])} 张"
    )

    out_contract = out_dir / CONTRACT
    out_contract.write_text(
        json.dumps(result.contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写出 {out_contract}：features={len(result.contract['features'])}")

    out_derivation = out_dir / DERIVATION
    out_derivation.write_text(result.derivation_md.rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_derivation}")

    _write_manifest(
        out_dir,
        args,
        "finalize",
        [
            {"name": "step2_2_feature_aggregation", "command_file": AGGREGATION_SQL, "collect_as": None},
        ],
        {
            "except_columns": result.plan["except_columns"],
            "list_fields": list_fields,
            "contract": CONTRACT,
            "derivation": DERIVATION,
            "validation_files": validation_files,
            "collect": {
                "name": "step2_2_validation_combined",
                "command_file": COMBINED_VALIDATION_SQL,
                "collect_as": RAW_WIDE_STATS,
            },
            "save_note": (
                SAVE_NOTE_HARD_RULES
                + "CREATE 无 collect；validation 只提交 combined SELECT，"
                f"save-collect 落到 {RAW_WIDE_STATS}。"
            ),
            "after_collect": _collect_guard(
                _gate_command(args), out_dir, expect_rows=True
            ),
        },
    )


def _sample(args: argparse.Namespace) -> None:
    """Pass 3: expand the String sampling query and record the candidate set.

    This is the branch point of the chain: with candidates the next command is
    ``string-plan``, without them (or when no sample SQL can be built) it skips
    straight to ``finalize``.
    """
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = _read_json(args.schema)
    meta = _read_json(args.meta)
    dump = _read_json(args.dump)
    list_fields = _read_list_fields(args.list_fields)
    policy = _load_policy()
    data_source = load_data_source(out_dir)

    candidates = string_cast_candidates(schema, meta, dump, list_fields, policy)
    out_candidates = out_dir / STRING_CANDIDATES
    out_candidates.write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写出 {out_candidates}：{len(candidates)} 个候选列")
    for candidate in candidates:
        print(
            f"  {candidate['requirement']:<8} {candidate['wide_column']}"
            f"  ({candidate['source_table']}.{candidate['source_column']})"
        )

    try:
        sample_sql = build_string_sample_query(schema, candidates, policy)
    except SystemExit as exc:
        print(str(exc))
        _write_manifest(
            out_dir,
            args,
            "sample",
            [],
            {
                "string_candidates": STRING_CANDIDATES,
                "after_collect": _collect_guard(_finalize_command(args), out_dir),
            },
        )
        return

    sample_sql = bind_placeholders(sample_sql, data_source)
    out_sample = out_dir / STRING_SAMPLE_SQL
    _assert_expanded(out_sample, sample_sql)
    out_sample.write_text(_non_comment_text(sample_sql).rstrip() + "\n", encoding="utf-8")
    print(f"已写出 {out_sample}")

    _write_manifest(
        out_dir,
        args,
        "sample",
        [
            {
                "name": "step2_2_string_sample",
                "command_file": STRING_SAMPLE_SQL,
                "collect_as": RAW_STRING_SAMPLE,
            }
        ],
        {
            "string_candidates": STRING_CANDIDATES,
            "after_collect": _collect_guard(
                _string_plan_command(args), out_dir, expect_rows=True
            ),
        },
    )


def _string_plan(args: argparse.Namespace) -> None:
    """Turn the sampled payload into ``string_cast_plan.txt`` plus a reviewable record."""
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    schema = _read_json(args.schema)
    meta = _read_json(args.meta)
    dump = _read_json(args.dump)
    list_fields = _read_list_fields(args.list_fields)
    policy = _load_policy()

    for path in args.raw_sample:
        if not path.is_file():
            raise SystemExit(f"未找到 String 采样 payload 文件：{path}")

    candidates = string_cast_candidates(schema, meta, dump, list_fields, policy)
    sample_rows = _rows(list(args.raw_sample))
    plan, notes = derive_string_plan(candidates, sample_rows, policy)

    lines = ["# <wide_column> <rule> [ratio]", "# 由 step2_2_driver.py string-plan 生成；改判定请编辑本文件后重跑 finalize"]
    for column, rule in plan.items():
        lines.append(f"{column} {rule}")
    out_plan = out_dir / STRING_PLAN
    out_plan.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写出 {out_plan}：{len(plan)} 条判定")
    for note in notes:
        print(f"  {note}")

    missing = [line for line in notes if "必须补采" in line]
    _write_manifest(
        out_dir,
        args,
        "string_plan",
        [],
        {
            "string_plan": STRING_PLAN,
            "unresolved": missing,
            "after_collect": _collect_guard(
                _finalize_command(args, STRING_PLAN), out_dir
            ),
        },
    )
    if missing:
        raise SystemExit(
            f"{len(missing)} 个必转列缺少采样结果，已写入 {STRING_PLAN} 的仅为可判定部分。"
        )


def _gate(args: argparse.Namespace) -> None:
    argv = ["--plan", str(args.plan), "--sql", str(args.sql), "--validation", str(args.validation)]
    if args.schema is not None:
        argv += ["--schema", str(args.schema)]
    if args.policy is not None:
        argv += ["--policy", str(args.policy)]
    if args.report_json is not None:
        argv += ["--report-json", str(args.report_json)]
    if args.marker is not None:
        argv += ["--marker", str(args.marker)]
    code = run_script(SCRIPTS_DIR / "step2_2_verify_gate.py", argv)
    if code == 0:
        _clear_after_collect(Path("."))
    raise SystemExit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="step2_2 driver (1:N aggregation)")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="pass 1: expand + bind the term-frequency SQL")
    prepare.add_argument("--schema", type=Path, required=True, help="schema_resolution.json")
    prepare.add_argument("--meta", type=Path, required=True, help="step1_output_meta.json")
    prepare.add_argument("--dump", type=Path, required=True, help="step2_0_profile_dump.json")
    prepare.add_argument("--list-fields", type=Path, default=Path("list_fields.txt"))
    prepare.add_argument("--out-dir", type=Path, default=Path("."))
    prepare.set_defaults(func=_prepare)

    finalize = sub.add_parser("finalize", help="pass 2: expand + bind aggregation/validation SQL")
    finalize.add_argument("--schema", type=Path, required=True)
    finalize.add_argument("--meta", type=Path, required=True)
    finalize.add_argument("--dump", type=Path, required=True)
    finalize.add_argument("--list-fields", type=Path, default=Path("list_fields.txt"))
    finalize.add_argument("--raw-terms", type=Path, action="append", default=[], help="term payload; repeatable")
    finalize.add_argument("--string-plan", type=Path, default=None, help="string_cast_plan.txt")
    finalize.add_argument("--out-dir", type=Path, default=Path("."))
    finalize.set_defaults(func=_finalize)

    sample = sub.add_parser("sample", help="pass 3: expand the String sampling query")
    sample.add_argument("--schema", type=Path, required=True)
    sample.add_argument("--meta", type=Path, required=True)
    sample.add_argument("--dump", type=Path, required=True)
    sample.add_argument("--list-fields", type=Path, default=Path("list_fields.txt"))
    sample.add_argument("--out-dir", type=Path, default=Path("."))
    sample.set_defaults(func=_sample)

    string_plan = sub.add_parser(
        "string-plan", help="turn sampled payload into string_cast_plan.txt"
    )
    string_plan.add_argument("--schema", type=Path, required=True)
    string_plan.add_argument("--meta", type=Path, required=True)
    string_plan.add_argument("--dump", type=Path, required=True)
    string_plan.add_argument("--list-fields", type=Path, default=Path("list_fields.txt"))
    string_plan.add_argument(
        "--raw-sample", type=Path, action="append", default=[], help="sample payload; repeatable"
    )
    string_plan.add_argument("--out-dir", type=Path, default=Path("."))
    string_plan.set_defaults(func=_string_plan)

    gate = sub.add_parser("gate", help="run the step2_2 gate (2a~2l)")
    gate.add_argument("--plan", type=Path, default=Path(PLAN))
    gate.add_argument("--sql", type=Path, default=Path(AGGREGATION_SQL))
    gate.add_argument("--validation", type=Path, default=Path(VALIDATION_SQL))
    gate.add_argument("--schema", type=Path, default=None)
    gate.add_argument("--policy", type=Path, default=None)
    gate.add_argument("--report-json", type=Path, default=None)
    gate.add_argument("--marker", type=Path, default=None)
    gate.set_defaults(func=_gate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
