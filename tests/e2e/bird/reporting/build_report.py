"""Build the BIRD report from completed case directories using read-only SQLite."""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from aggregate_observations import classify
from score_candidates import _execute, _projection, compare, score_case

HERE = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_bytes())


def write(path, obj):
    raw = (
        "".join(json.dumps(v) + "\n" for v in obj)
        if path.suffix == ".jsonl"
        else json.dumps(obj, indent=2) + "\n"
    )
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(raw)
    tmp.replace(path)


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_tool(script, *args):
    subprocess.run(
        [sys.executable, str(HERE / script), *map(str, args)],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def extra_scores(requests, gold, dbs, cases):
    by_q = defaultdict(list)
    for line in requests.read_text().splitlines():
        r = json.loads(line)
        by_q[r["qid"]].append(r)
    scores = []
    for qid, pending in by_q.items():
        q = gold[qid]
        db = dbs / q["db_id"] / (q["db_id"] + ".sqlite")
        gh = hashlib.sha256(q["SQL"].encode()).hexdigest()
        try:
            rows, width, _ = _execute(db, q["SQL"], 120)
            expected = set(map(tuple, rows))
        except (TimeoutError, ValueError, sqlite3.Error):
            continue
        for r in pending:
            if q["db_id"] != r["db_id"] or gh != r["gold_sha256"]:
                raise ValueError(f"database or gold mismatch for {qid}")
            blob = Path(r["protected_refs"][0]["path"]).resolve()
            blobdir = Path(cases[qid]["source_case_dir"]) / "observation-v1/blobs"
            if blob.parent != blobdir.resolve() or file_hash(blob) != r["sql_sha256"]:
                raise ValueError(f"score blob identity mismatch for {qid}")
            strict = reference = None
            try:
                actual, actual_width, _ = _execute(db, blob.read_text(), 120)
                obtained = set(map(tuple, actual))
                strict = expected == obtained
                reference = strict or (
                    actual_width > width
                    and _projection(expected, obtained, width, actual_width, 100000, 5) is not None
                )
            except TimeoutError:
                pass
            except Exception:
                if strict is None:
                    strict = reference = False
            scores.append(
                {k: r[k] for k in ("qid", "db_id", "gold_sha256", "sql_sha256")}
                | {"strict": strict, "reference": reference}
            )
    return scores


def whole_chain(labels, inv_sha):
    modes = ("strict", "reference")
    keys = ("Fixed", "Broke", "KeptR", "KeptW", "Unknown")
    effects = {m: Counter(dict.fromkeys(keys, 0)) for m in modes}
    removed = {m: Counter() for m in modes}
    dropped = kept = unpaired = 0
    for row in labels:
        stages = row["stages"]
        if any(stages[s].get("missing", False) for s in ("Generation", "AfterValidation")):
            unpaired += 1
            continue
        candidates = stages["Generation"]["candidates"]
        validated = stages["AfterValidation"]["candidates"]
        before = {c["id"]: c for c in candidates}
        after = {c["id"]: c for c in validated}
        if (
            None in before
            or None in after
            or len(before) != len(candidates)
            or len(after) != len(validated)
            or not after.keys() <= before.keys()
        ):
            raise ValueError("candidate identity mismatch qid=" + str(row["qid"]))
        for cid, initial in before.items():
            if cid in after:
                kept += 1
                for m in modes:
                    effects[m][classify(initial[m], after[cid][m])] += 1
            else:
                dropped += 1
                for m in modes:
                    removed[m][str(initial[m])] += 1
    return {
        "inventory_sha256": inv_sha,
        "whole_chain_retained_effect": {"candidate_count": kept, **effects},
        "dropped_candidates": dropped,
        "dropped_before_labels": removed,
        "unpaired_questions": unpaired,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-dir",
        required=True,
        type=Path,
        action="append",
        help="Frozen partition/revision, earliest first",
    )
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--databases", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-slots", type=int, default=9)
    args = parser.parse_args()
    os.umask(0o077)
    db_file, dbs, dest = args.dataset, args.databases, args.output
    if dest.exists():
        parser.error("--output must be a new directory")
    rows = read(db_file)
    gold = {q["question_id"]: q for q in rows}
    if len(gold) != len(rows):
        parser.error("duplicate dataset question ID")
    cases = {}
    for run_dir in args.run_dir:
        directories = sorted((run_dir.resolve() / "cases").glob("*/stats.json"))
        if not directories:
            parser.error(f"no completed case artifacts in {run_dir}")
        for stats_path in directories:
            stats = read(stats_path)
            if stats.get("completed") is not True:
                continue
            qid = stats["question_id"]
            path = stats_path.parent
            if (
                qid not in gold
                or stats["db_id"] != gold[qid]["db_id"]
                or path.name != f"{stats['db_id']}__q{qid}"
            ):
                raise ValueError(f"dataset does not match case {path.name}")
            case_input = read(path / "input.json")
            if any(
                case_input.get(key, "") != gold[qid].get(key, "")
                for key in ("db_id", "question_id", "question", "evidence")
            ):
                raise ValueError(f"frozen input differs from supplied dataset for {path.name}")
            expected_gold = hashlib.sha256(gold[qid]["SQL"].encode()).hexdigest()
            recorded_gold = stats.get("gold_sha256") or case_input.get("gold_sha256")
            if recorded_gold and recorded_gold != expected_gold:
                raise ValueError(f"gold SQL differs for {path.name}")
            verdict = stats
            if not recorded_gold and not stats.get("error"):
                dbid = stats["db_id"]
                verdict = compare(
                    dbs / dbid / f"{dbid}.sqlite",
                    (path / "predicted.sql").read_text(),
                    gold[qid]["SQL"],
                )
            cases[qid] = {
                "qid": qid,
                "case_id": path.name,
                "source_case_dir": str(path),
                "source_stats": str(stats_path),
                "stats_sha256": file_hash(stats_path),
                "normal": not bool(stats.get("error")),
                "strict_correct": verdict["strict_correct"],
                "extra_columns_correct": verdict["extra_columns_correct"],
            }
    if not cases:
        parser.error("no terminal cases")
    dest.mkdir(parents=True)
    inv = {"cases": list(cases.values()), "gold_sha256": file_hash(db_file)}
    entries, labels = [], []
    for qid, case in cases.items():
        if not case["normal"]:
            continue
        path = Path(case["source_case_dir"])
        row = score_case(case, gold, dbs)
        labels.append(row)
        events = path / "observation-v1/events.jsonl"
        errors = []
        model_events = path / "model-events.jsonl"
        for line in model_events.read_text().splitlines() if model_events.is_file() else ():
            if json.loads(line).get("event") == "capture_error":
                errors.append("model_capture_error")
        if events.exists():
            entries.append(
                {
                    "qid": qid,
                    "case_id": case["case_id"],
                    "stats_sha256": case["stats_sha256"],
                    "events_path": str(events),
                    "events_sha256": file_hash(events),
                    "generator_complete": not row["stages"]["Generation"].get("missing", False),
                    "capture_errors": errors,
                }
            )
    ip, lp, mp = (
        dest / name
        for name in (
            "effective-inventory.json",
            "scored-percase.jsonl",
            "events-manifest.json",
        )
    )
    write(ip, inv)
    write(lp, labels)
    write(mp, {"inventory_sha256": file_hash(ip), "cases": entries})
    audit = dest / "audits"
    common = [
        "--inventory",
        ip,
        "--dataset",
        db_file,
        "--labels",
        lp,
        "--target-slots",
        args.target_slots,
    ]
    agg = [
        "aggregate_observations.py",
        *common,
        "--events-manifest",
        mp,
        "--output",
        audit,
    ]
    run_tool(*agg)
    pending = audit / "pending-score-requests.jsonl"
    if pending.stat().st_size:
        sm = dest / "extra-score-map.jsonl"
        write(sm, extra_scores(pending, gold, dbs, cases))
        run_tool(*agg, "--score-map", sm)
    write(audit / "whole-chain.json", whole_chain(labels, file_hash(ip)))
    run_tool(
        "render_report.py",
        *common,
        "--stages",
        "Generation,AfterValidation",
        "--generator-audit",
        audit / "generator-summary.json",
        "--checker-audit",
        audit / "checker-summary.json",
        "--whole-chain-audit",
        audit / "whole-chain.json",
        "--output",
        dest / "four-section",
    )
    print(
        json.dumps(
            {
                "scored_terminal": len(labels),
                "event_cases": len(entries),
                "pending_extra_scores": read(audit / "coverage.json")["pending_score_count"],
            }
        )
    )


if __name__ == "__main__":
    main()
