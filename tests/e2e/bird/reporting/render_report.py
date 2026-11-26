"""Render four-section BIRD reports from frozen, already-scored safe artifacts.
No network, model, SQL execution, or inspection of raw model archives.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import tempfile
from pathlib import Path

STAGES = ("Generation", "AfterValidation")
EFFECTS = ("Fixed", "Broke", "KeptR", "KeptW", "Unknown")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text()) if path else None


def read_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def qid(row):
    raw = row.get("qid", row.get("question_id"))
    return int(str(raw).removeprefix("q_"))


def table(headers, rows):
    def cell(x):
        return ("NA" if x is None else str(x)).replace("|", "&#124;").replace("\n", "<br>")

    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
        ]
        + ["| " + " | ".join(cell(x) for x in r) + " |" for r in rows]
    )


def percentage(value):
    return "NA" if value is None else f"{value * 100:.2f}%"


def count_rate(count, total):
    return f"{count}/{total}（{percentage(count / total if total else None)}）"


def stage_stats(rows, stage):
    n = len(rows)
    result = {
        "questions": n,
        "candidate_slots": sum(len(r["stages"][stage]["candidates"]) for r in rows),
    }
    for label in ("strict", "reference"):
        known_oracle = potential = unknown_slots = 0
        low_sum = high_sum = 0.0
        top_correct = top_unknown = 0
        for row in rows:
            s = row["stages"][stage]
            candidates = s["candidates"]
            values = [c.get(label) for c in candidates]
            if any(v is not None and not isinstance(v, bool) for v in values):
                raise ValueError("candidate labels must be bool or null")
            k, u, slots = values.count(True), values.count(None), len(values)
            unknown_slots += u
            if k:
                known_oracle += 1
                low_sum += k / slots
                high_sum += (k + u) / slots
            elif u:
                potential += 1
            top = s.get("top1_" + label, s.get("vote_" + label))
            if stage == "AfterValidation" and "top1_" + label not in s:
                top = row.get("strict_top1" if label == "strict" else "reference_top1", top)
            if top is not None and not isinstance(top, bool):
                raise ValueError("top1 labels must be bool or null")
            top_correct += top is True
            top_unknown += top is None
        denominator = known_oracle + potential
        result[label] = {
            "top1_correct": top_correct,
            "top1_unknown": top_unknown,
            "oracle_correct": known_oracle,
            "oracle_potential": potential,
            "oracle_q_lower": known_oracle / n if n else None,
            "oracle_q_upper": denominator / n if n else None,
            "oracle_sql_lower": low_sum / denominator if denominator else None,
            "oracle_sql_upper": (high_sum + potential) / denominator if denominator else None,
            "unknown_slots": unknown_slots,
        }
    return result


def normalize_generator(audit, target):
    if not audit:
        return None
    out = dict(audit)
    legacy = {
        "first_round_full9_cases": "first_full_cases",
        "final_full9_cases": "final_full_cases",
        "filled_to_full9_cases": "retry_filled_cases",
    }
    if any(k in audit for k in legacy) and target != 9:
        raise ValueError("legacy full9 audit requires target_slots=9")
    for old, new in legacy.items():
        if new not in out and old in out:
            out[new] = out[old]
    out.setdefault("logical_calls", out.get("generator_logical_calls"))
    return out


def normalize_checker(audit):
    if not audit:
        return {}
    # Legacy event audit is an observable subset, not all condition triggers.
    if "confirmed_adoption_matrix" in audit:
        return {
            name: {
                **row,
                "observable_effect_events": row["events"],
                "triggered": None,
                "coverage_note": "可恢复实际效果子集；全部触发次数未知",
            }
            for name, row in audit["confirmed_adoption_matrix"].items()
        }
    return audit.get("per_checker", {})


def generate(args):
    inventory = load(args.inventory)
    items = inventory["cases"]
    if not items:
        raise ValueError("empty inventory")
    inventory_sha = digest(args.inventory)
    by_id = {qid(c): c for c in items}
    if len(by_id) != len(items):
        raise ValueError("duplicate inventory qid")
    dataset = load(args.dataset)
    gold = {qid(q): q for q in dataset}
    if len(gold) != len(dataset) or set(by_id) - set(gold):
        raise ValueError("dataset qid mapping invalid")
    gold_sha = digest(args.dataset)
    if inventory.get("gold_sha256") != gold_sha:
        raise ValueError("dataset SHA differs from frozen inventory")
    scored = read_rows(args.labels)
    score_by_id = {qid(r): r for r in scored}
    if len(score_by_id) != len(scored) or set(score_by_id) - set(by_id):
        raise ValueError("duplicate or out-of-cohort scored qid")
    identity_unverified = []
    for ident, row in score_by_id.items():
        expected_gold = hashlib.sha256(gold[ident]["SQL"].encode()).hexdigest()
        if row.get("gold", {}).get("sha256") != expected_gold:
            raise ValueError(f"Gold SQL hash mismatch q_{ident:04}")
        frozen_sha = by_id[ident].get("stats_sha256")
        if frozen_sha and row.get("stats_sha256") != frozen_sha:
            raise ValueError(f"stats revision mismatch q_{ident:04}")
        if not frozen_sha:
            identity_unverified.append(ident)
    stages = tuple(args.stages.split(","))
    if not stages or any(s not in STAGES for s in stages):
        raise ValueError("unsupported stages")
    # Same normal question cohort for comparisons; missing stage != zero candidates.
    normal = {i for i, c in by_id.items() if c.get("normal") is True}
    common = [
        score_by_id[i]
        for i in sorted(normal)
        if i in score_by_id
        and all(
            isinstance(score_by_id[i].get("stages", {}).get(s), dict)
            and score_by_id[i]["stages"][s].get("missing") is not True
            and isinstance(score_by_id[i]["stages"][s].get("candidates"), list)
            for s in stages
        )
    ]
    metrics = {s: stage_stats(common, s) for s in stages}
    generator_rows = [
        r
        for r in scored
        if isinstance(r.get("stages", {}).get("Generation", {}).get("candidates"), list)
        and not r["stages"]["Generation"].get("missing")
    ]
    dist = collections.Counter(len(r["stages"]["Generation"]["candidates"]) for r in generator_rows)
    generator = normalize_generator(load(args.generator_audit), args.target_slots)
    checker_raw = load(args.checker_audit)
    checker = normalize_checker(checker_raw)
    whole_chain = load(args.whole_chain_audit)
    warnings = []
    for name, data in [
        ("generator", generator),
        ("checker", checker_raw),
        ("whole_chain", whole_chain),
    ]:
        if data:
            if data.get("inventory_sha256") and data["inventory_sha256"] != inventory_sha:
                raise ValueError(name + " audit inventory mismatch")
            if not data.get("inventory_sha256"):
                warnings.append(name + " audit 未携带 inventory_sha256，不能自动确认同快照")
    actual_slots = sum(k * n for k, n in dist.items())
    audit_cases = (generator or {}).get("covered_cases", (generator or {}).get("cases"))
    if generator and audit_cases != len(generator_rows):
        warnings.append(
            f"Generator重试审计覆盖{audit_cases if audit_cases is not None else '未知'}题，产出标签覆盖{len(generator_rows)}题；审计计数不外推"
        )
    if generator and audit_cases == len(generator_rows):
        if (
            generator.get("final_successful_slots") is not None
            and generator["final_successful_slots"] != actual_slots
        ):
            raise ValueError("generator audit final slot count disagrees with labels")
        if (
            generator.get("success_attempt_distribution")
            and sum(generator["success_attempt_distribution"].values()) != actual_slots
        ):
            raise ValueError("generator successful attempt distribution mismatch")
    for name, row in checker.items():
        for mode in ("strict", "reference"):
            counts = row.get(mode)
            if not counts:
                continue
            if any(not isinstance(counts.get(k, 0), int) or counts.get(k, 0) < 0 for k in EFFECTS):
                raise ValueError("invalid checker effect counts")
            n = row.get("observable_effect_events", row.get("events", row.get("triggered")))
            if n is not None and sum(counts.get(k, 0) for k in EFFECTS) != n:
                raise ValueError("checker matrix coverage mismatch: " + name)
    metadata = load(args.metadata) or {}
    safe_parameters = {
        k: v
        for k, v in metadata.get("model_parameters", {}).items()
        if k
        in (
            "temperature",
            "reasoning_effort",
            "max_tokens",
            "max_completion_tokens",
            "enable_thinking",
            "stream",
            "include_usage",
        )
    }
    report = {
        "schema_version": 1,
        "inventory_sha256": inventory_sha,
        "gold_sha256": gold_sha,
        "cohort_total": len(items),
        "normal_total": len(normal),
        "common_evaluated": len(common),
        "not_normal_qids": sorted(set(by_id) - normal),
        "normal_without_all_scored_stages": sorted(normal - {qid(r) for r in common}),
        "stages_run": stages,
        "metrics": metrics,
        "generator_distribution": dict(sorted(dist.items())),
        "generator_observed_cases": len(generator_rows),
        "generator_unobserved_cases": len(items) - len(generator_rows),
        "generator_target_slots": args.target_slots,
        "generator_actual_slots": actual_slots,
        "generator_audit": generator,
        "checker_rows": checker,
        "checker_audit": checker_raw,
        "whole_chain": whole_chain,
        "identity_unverified_qids": identity_unverified,
        "warnings": warnings,
    }
    lines = [
        "# BIRD 评测四节报告",
        "## 1. 数据集",
        table(
            ["项目", "值"],
            [
                ["题库", metadata.get("dataset_name", Path(args.dataset).name)],
                ["题库 SHA256", gold_sha],
                ["清单 SHA256", inventory_sha],
                ["源码提交", metadata.get("source_commit")],
                ["模型", metadata.get("model")],
                [
                    "模型参数",
                    json.dumps(safe_parameters, ensure_ascii=False) if safe_parameters else None,
                ],
                [
                    "复用节点",
                    ", ".join(metadata.get("cached_nodes", []))
                    if "cached_nodes" in metadata
                    else None,
                ],
                ["快照", inventory.get("client_end_utc", metadata.get("snapshot_utc"))],
                ["缺少可核验stats SHA题数", len(identity_unverified)],
                [
                    "总题数 / 正常 / 可配对评分",
                    f"{len(items)} / {len(normal)} / {len(common)}",
                ],
                [
                    "非正常或未完成题",
                    ", ".join(f"q_{i:04}" for i in report["not_normal_qids"]) or "无",
                ],
            ],
        ),
        "按冻结清单选择最终整题记录，不挑分；本入口不运行模型、SQL或评分，仅校验来源并汇总已评分标签。",
        "## 2. Generation 与 After Validation 准确率",
    ]
    rows = []
    for stage in STAGES:
        if stage not in stages:
            rows.append([stage] + ["未运行"] * 6)
            continue
        m = metrics[stage]
        vals = []
        for label in ("strict", "reference"):
            d = m[label]
            top = count_rate(d["top1_correct"], len(common))
            if d["top1_unknown"]:
                top += f"；未决{d['top1_unknown']}"
            oracle = count_rate(d["oracle_correct"], len(common))
            if d["oracle_potential"]:
                oracle += f"；另{d['oracle_potential']}题未决"
            macro = percentage(d["oracle_sql_lower"])
            if d["oracle_sql_lower"] != d["oracle_sql_upper"]:
                macro += "–" + percentage(d["oracle_sql_upper"])
            macro += f"（已证{d['oracle_correct']}题）"
            vals += [top, oracle, macro]
        rows.append([stage] + vals)
    lines += [
        table(
            [
                "阶段",
                "严格投票Top1",
                "严格Oracle Q",
                "严格条件Oracle SQL",
                "参考投票Top1",
                "参考Oracle Q",
                "参考条件Oracle SQL",
            ],
            rows,
        ),
        f"两阶段使用相同可评正常题分母 {len(common)}；非正常/缺评分题不默认为错误或补成零。投票标签须由上游按实际结果簇与平票规则计算，本入口不根据正确性挑选胜者。",
        "Oracle Q是至少一个正确候选的题目占比；条件Oracle SQL仅对有正确候选题，平均每题“正确槽/实际槽”。重复SQL保留。未知标签产生区间或未决，不硬判错。",
        "## 3. Generator 首次生成、重试与最终条数",
    ]
    keys = [
        ("first_full_cases", "首次满槽题数"),
        ("final_full_cases", "最终满槽题数"),
        ("retry_filled_cases", "重试补满题数"),
        ("cases_with_any_slot_recovered", "至少补回一槽题数"),
        ("initial_successful_slots", "首次成功槽数"),
        ("internally_recovered_slots", "重试补回槽数"),
        ("logical_calls", "槽内逻辑调用"),
        ("internal_retry_calls", "槽内后续逻辑调用"),
        ("whole_case_retries", "历史整题补跑"),
        ("sdk_physical_attempts", "SDK物理尝试"),
    ]
    lines += [
        table(
            ["指标", "数量"],
            [[title, generator.get(key) if generator else None] for key, title in keys],
        ),
        table(
            ["第几次逻辑调用成功", "成功槽数"],
            sorted(
                (generator or {}).get("success_attempt_distribution", {}).items(),
                key=lambda x: int(x[0]),
            )
            or [["NA", "NA"]],
        ),
        table(
            ["最终SQL槽数", "题数", "占全题单"],
            [
                [n, dist.get(n, 0), percentage(dist.get(n, 0) / len(items))]
                for n in range(args.target_slots + 1)
            ]
            + (
                [
                    [
                        f">{args.target_slots}",
                        sum(v for k, v in dist.items() if k > args.target_slots),
                        percentage(
                            sum(v for k, v in dist.items() if k > args.target_slots) / len(items)
                        ),
                    ]
                ]
                if any(k > args.target_slots for k in dist)
                else []
            )
            + [
                [
                    "未观察到Generator输出",
                    len(items) - len(generator_rows),
                    percentage((len(items) - len(generator_rows)) / len(items)),
                ]
            ],
        ),
        f"重试审计覆盖{audit_cases if audit_cases is not None else 'NA'}题；已观测 {len(generator_rows)} 题、{actual_slots} 个槽；未观察到不等于生成0槽。目标每题{args.target_slots}槽，实际不足不补齐。整题补跑、槽内逻辑调用及SDK物理重试是不同层级；无审计数据的项为NA。",
        "## 4. Checker 修复效果",
    ]
    crows = []
    for name, row in checker.items():
        c = row.get("strict") or {}
        crows.append(
            [
                name,
                row.get("triggered"),
                row.get("observable_effect_events", row.get("events")),
                row.get("not_triggered"),
                row.get("skipped"),
                row.get("errors"),
            ]
            + [c.get(k) for k in EFFECTS]
            + [c.get("Fixed", 0) - c.get("Broke", 0) if c else None]
        )
    if crows:
        total_cells = []
        for column in zip(*(row[1:] for row in crows)):
            total_cells.append(sum(column) if all(isinstance(x, int) for x in column) else None)
        crows.append(["TOTAL（对应观测范围）"] + total_cells)
    lines += [
        table(
            [
                "Checker",
                "Triggered",
                "可评分观测事件",
                "未触发",
                "跳过",
                "错误",
                "Fixed",
                "Broke",
                "KeptR",
                "KeptW",
                "Unknown",
                "NetEff",
            ],
            crows or [["未运行或无审计"] + ["NA"] * 12],
        ),
        "矩阵只使用实际输入与实际采用输出；拒绝保留输入须使用原输入，回复未证实采用不得并入。Triggered和可评分观测事件可能不同；未触发/跳过/错误不可由缺日志推算。共享模板无法归属时单列，不分摊到具体checker。",
    ]
    if checker_raw:
        lines += [
            table(
                ["覆盖/边界", "数量"],
                [
                    [k, checker_raw.get(k)]
                    for k in (
                        "confirmed_effect_events",
                        "reply_only_events",
                        "rejected_reply_retained_input_events",
                        "unknown_score_sides",
                    )
                ],
            )
        ]
    chain = (whole_chain or {}).get(
        "whole_chain_retained_effect", (whole_chain or {}).get("retained_effect")
    )
    if chain:
        lines += [
            "### 全链首尾（与逐事件不同口径）",
            table(
                ["口径", "保留候选", "Fixed", "Broke", "KeptR", "KeptW", "Unknown"],
                [
                    [mode, chain.get("candidate_count")]
                    + [chain.get(mode, {}).get(k) for k in EFFECTS]
                    for mode in ("strict", "reference")
                ],
            ),
        ]
    else:
        lines += ["全链首尾矩阵：未运行或证据不足，NA。"]
    if whole_chain:
        lines += [
            table(
                ["丢弃候选数", "丢弃前严格标签", "丢弃前参考标签"],
                [
                    [
                        whole_chain.get("dropped_candidates"),
                        json.dumps(
                            whole_chain.get("dropped_before_labels", {}).get("strict"),
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            whole_chain.get("dropped_before_labels", {}).get("reference"),
                            ensure_ascii=False,
                        ),
                    ]
                ],
            )
        ]
    lines += ["覆盖提示：" + ("；".join(warnings) if warnings else "输入快照校验通过。")]
    files = {
        "inventory": args.inventory,
        "dataset": args.dataset,
        "labels": args.labels,
        "generator_audit": args.generator_audit,
        "checker_audit": args.checker_audit,
        "whole_chain_audit": args.whole_chain_audit,
        "metadata": args.metadata,
    }
    verification = {
        "schema_version": 1,
        "status": "RENDERED_FROM_SCORED_ARTIFACTS",
        "inputs": {
            k: {"path": str(Path(p).resolve()), "sha256": digest(p)} for k, p in files.items() if p
        },
        "checks": {
            "unique_inventory_qids": True,
            "unique_label_qids": True,
            "gold_hashes": True,
            "available_stats_hashes": True,
            "audit_counts_checked": True,
        },
        "unverified_stats_qids": identity_unverified,
        "warnings": warnings,
    }
    return report, "\n\n".join(lines) + "\n", verification


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        f.write(value)
        temporary = Path(f.name)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("inventory", "dataset", "labels", "output"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--target-slots", type=int, required=True)
    parser.add_argument("--stages", default="Generation,AfterValidation")
    for key in ("generator-audit", "checker-audit", "whole-chain-audit", "metadata"):
        parser.add_argument("--" + key)
    args = parser.parse_args()
    if args.target_slots < 1:
        parser.error("target slots must be positive")
    report, md, verification = generate(args)
    out = Path(args.output)
    atomic_write(out / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
    atomic_write(out / "report.md", md)
    verification["outputs"] = {n: digest(out / n) for n in ("report.md", "report.json")}
    atomic_write(
        out / "verification.json",
        json.dumps(verification, ensure_ascii=False, indent=2),
    )
    print(
        json.dumps(
            {
                "output": str(out.resolve()),
                "cohort": report["cohort_total"],
                "evaluated": report["common_evaluated"],
                "warnings": verification["warnings"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
