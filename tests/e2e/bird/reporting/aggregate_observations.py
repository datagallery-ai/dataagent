"""Aggregate outcome-v1 metadata using supplied scores; never read SQL blobs/run SQL/models."""

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path

from render_report import digest, load, qid, read_rows


def classify(a, b):
    if a is None or b is None:
        return "Unknown"
    return "KeptR" if a and b else "Broke" if a else "Fixed" if b else "KeptW"


def aggregate(args):
    inv = load(args.inventory)
    frozen = {qid(c): c for c in inv["cases"]}
    if len(frozen) != len(inv["cases"]):
        raise ValueError("duplicate inventory qid")
    inventory_sha = digest(args.inventory)
    data = load(args.dataset)
    if digest(args.dataset) != inv["gold_sha256"]:
        raise ValueError("dataset SHA mismatch")
    gold = {qid(q): q for q in data}
    gold_hash = {i: hashlib.sha256(q["SQL"].encode()).hexdigest() for i, q in gold.items()}
    labels = read_rows(args.labels)
    label_by = {qid(r): r for r in labels}
    if len(labels) != len(label_by) or set(label_by) - set(frozen):
        raise ValueError("label cohort invalid")
    cache = {}
    pending = {}
    issues = []

    def add_score(i, db, gh, sh, strict, reference):
        if i not in frozen or db != gold[i]["db_id"] or gh != gold_hash[i]:
            raise ValueError("score Gold/DB/qid mismatch")
        if any(x is not None and not isinstance(x, bool) for x in [strict, reference]):
            raise ValueError("invalid score labels")
        key = (i, db, gh, sh)
        value = {"strict": strict, "reference": reference}
        if key in cache:
            for field in value:
                old = cache[key][field]
                new = value[field]
                if old is not None and new is not None and old != new:
                    raise ValueError("conflicting score cache")
                if new is None:
                    value[field] = old
        cache[key] = value

    for i, r in label_by.items():
        if r.get("stats_sha256") != frozen[i].get("stats_sha256"):
            raise ValueError("label stats revision mismatch")
        if r.get("gold", {}).get("sha256") != gold_hash[i]:
            raise ValueError("label Gold hash mismatch")
        for s in r.get("stages", {}).values():
            for c in s.get("candidates", []):
                add_score(
                    i,
                    gold[i]["db_id"],
                    gold_hash[i],
                    c["sql_sha256"],
                    c.get("strict"),
                    c.get("reference"),
                )
    for path in args.score_map:
        for r in read_rows(path):
            add_score(
                qid(r),
                r["db_id"],
                r["gold_sha256"],
                r["sql_sha256"],
                r.get("strict"),
                r.get("reference"),
            )
    manifest = load(args.events_manifest)
    if manifest["inventory_sha256"] != inventory_sha:
        raise ValueError("event manifest inventory mismatch")
    event_cases = manifest["cases"]
    seen = set()
    slot_success = collections.Counter()
    initial_slots = initial_full = filled = any_recovered = covered = 0
    generator_calls = retry_calls = 0
    model_call_ids = set()
    retry_model_ids = set()
    checker = {}
    effects = []
    generator_case_rows = []

    def score(i, ref, purpose):
        if (
            not isinstance(ref, dict)
            or not isinstance(ref.get("sha256"), str)
            or len(ref["sha256"]) != 64
        ):
            issues.append({"qid": i, "reason": "missing_sql_reference", "purpose": purpose})
            return {"strict": None, "reference": None}
        key = (i, gold[i]["db_id"], gold_hash[i], ref["sha256"])
        v = cache.get(key, {"strict": None, "reference": None})
        if any(x is None for x in v.values()):
            p = pending.setdefault(
                key,
                {
                    "qid": i,
                    "db_id": gold[i]["db_id"],
                    "gold_sha256": gold_hash[i],
                    "sql_sha256": ref["sha256"],
                    "protected_refs": [],
                    "purposes": [],
                    "known_scores": v,
                },
            )
            if ref not in p["protected_refs"]:
                p["protected_refs"].append(ref)
            if purpose not in p["purposes"]:
                p["purposes"].append(purpose)
        return v

    for entry in event_cases:
        i = qid(entry)
        if i not in frozen or i in seen:
            raise ValueError("event case cohort invalid")
        seen.add(i)
        if entry.get("stats_sha256") != frozen[i].get("stats_sha256"):
            raise ValueError("events stats revision mismatch")
        path = Path(entry["events_path"])
        if digest(path) != entry["events_sha256"]:
            raise ValueError("events SHA mismatch")
        events = read_rows(path)
        if any(e.get("version") != 1 or e.get("case_id") != entry["case_id"] for e in events):
            raise ValueError("event version/case mismatch")
        if entry.get("capture_errors"):
            issues.append(
                {
                    "qid": i,
                    "reason": "capture_errors",
                    "count": len(entry["capture_errors"]),
                }
            )
        generators = collections.defaultdict(list)
        checkers = collections.defaultdict(list)
        for e in events:
            if e.get("event", "").startswith("generator_"):
                identity = e.get("invocation_id")
                if identity:
                    generators[identity].append(e)
                else:
                    issues.append({"qid": i, "reason": "generator_missing_invocation_id"})
            if e.get("checker") is not None:
                identity = e.get("checker_invocation_id")
                if identity:
                    checkers[identity].append(e)
                else:
                    issues.append({"qid": i, "reason": "checker_missing_invocation_id"})
        for e in events:
            if (
                e.get("event") == "model_call"
                and e.get("invocation_id") in generators
                and e.get("logical_call_id")
            ):
                key = (i, e["logical_call_id"])
                model_call_ids.add(key)
                if isinstance(e.get("attempt_index"), int) and e["attempt_index"] > 1:
                    retry_model_ids.add(key)
        final = label_by.get(i, {}).get("stages", {}).get("Generation", {}).get("candidates")
        case_ok = (
            bool(generators)
            and entry.get("generator_complete") is True
            and not entry.get("capture_errors")
            and isinstance(final, list)
        )
        finalmap = {str(c.get("id")): c for c in final or []}
        if len(finalmap) != len(final or []):
            case_ok = False
        mappings = {}
        case_attempts = []
        for ident, es in generators.items():
            enters = [e for e in es if e["event"] == "generator_enter"]
            ends = [e for e in es if e["event"] in ("generator_result", "generator_error")]
            attempts = [e.get("attempt_index") for e in es if e["event"] == "generator_attempt"]
            if (
                len(enters) != 1
                or (
                    len(enters) == 1
                    and (enters[0].get("slot_id") is None or enters[0].get("strategy") is None)
                )
                or len(ends) != 1
                or any(not isinstance(a, int) or a < 1 for a in attempts)
                or len(attempts) != len(set(attempts))
            ):
                case_ok = False
            accepted = [e for e in es if e["event"] == "generator_accepted"]
            if len(accepted) > 1:
                case_ok = False
            accepted_attempt = accepted[0].get("attempt_index") if len(accepted) == 1 else None
            if accepted_attempt is not None and accepted_attempt not in attempts:
                case_ok = False
            generator_calls += len(attempts)
            retry_calls += sum(a > 1 for a in attempts if isinstance(a, int))
            refs = (
                collections.Counter(
                    r.get("sha256") for r in accepted[0].get("output_refs", []) if r
                )
                if accepted
                else collections.Counter()
            )
            for e in es:
                if e["event"] != "generator_candidate_mapping":
                    continue
                cid = str(e.get("candidate_id"))
                ref = e.get("output_ref") or {}
                sh = ref.get("sha256")
                if (
                    not e.get("identity_known")
                    or cid in mappings
                    or cid not in finalmap
                    or finalmap[cid].get("sql_sha256") != sh
                    or refs[sh] < 1
                    or accepted_attempt is None
                ):
                    case_ok = False
                    issues.append(
                        {
                            "qid": i,
                            "reason": "candidate_mapping_unknown_or_post_hook_changed",
                            "candidate_id": e.get("candidate_id"),
                        }
                    )
                    continue
                refs[sh] -= 1
                mappings[cid] = accepted_attempt
                case_attempts.append(accepted_attempt)
        if set(mappings) != set(finalmap):
            case_ok = False
        if case_ok:
            covered += 1
            first = sum(a == 1 for a in case_attempts)
            after = len(final)
            initial_slots += first
            initial_full += first == args.target_slots
            filled += first < args.target_slots and after == args.target_slots
            any_recovered += any(a > 1 for a in case_attempts)
            slot_success.update(map(str, case_attempts))
        else:
            issues.append({"qid": i, "reason": "generator_retry_coverage_incomplete"})
        generator_case_rows.append(
            {
                "qid": i,
                "exact_retry_mapping": case_ok,
                "final_candidate_slots": len(final) if isinstance(final, list) else None,
                "first_success_attempts": case_attempts if case_ok else None,
            }
        )
        for ident, es in checkers.items():
            names = {e.get("checker") for e in es}
            enters = [e for e in es if e["event"] == "checker_enter"]
            ends = [e for e in es if e["event"] == "checker_exit"]
            errors = [e for e in es if e["event"] == "checker_error"]
            if len(names) != 1 or len(enters) != 1 or len(ends) + len(errors) != 1:
                issues.append(
                    {
                        "qid": i,
                        "reason": "checker_invocation_incomplete_or_ambiguous",
                        "checker_invocation_id": ident,
                    }
                )
                continue
            name = next(iter(names))
            row = checker.setdefault(
                name,
                {
                    "triggered": None,
                    "not_triggered": None,
                    "skipped": None,
                    "errors": 0,
                    "entered_invocations": 0,
                    "observed_repair_invocations": 0,
                    "repair_trigger_events": 0,
                    "deterministic_changed_invocations": 0,
                    "observable_effect_events": 0,
                    "no_observed_repair_or_change": 0,
                    "parse_attempts": 0,
                    "parse_rejected": 0,
                    "model_calls": 0,
                    "sample_batches": 0,
                    "parsed_outputs_not_adopted": 0,
                    "rejected_reply_count": None,
                    "discarded": None,
                    "strict": dict.fromkeys(["Fixed", "Broke", "KeptR", "KeptW", "Unknown"], 0),
                    "reference": dict.fromkeys(["Fixed", "Broke", "KeptR", "KeptW", "Unknown"], 0),
                },
            )
            repair = sum(e["event"] == "repair_trigger" for e in es)
            row["entered_invocations"] += 1
            row["errors"] += bool(errors)
            row["repair_trigger_events"] += repair
            row["observed_repair_invocations"] += repair > 0
            row["parse_attempts"] += sum(e["event"] == "checker_parse" for e in es)
            row["parse_rejected"] += sum(
                e["event"] == "checker_parse" and e.get("accepted") is False for e in es
            )
            row["model_calls"] += len(
                {
                    e["logical_call_id"]
                    for e in es
                    if e["event"] == "model_call" and e.get("logical_call_id")
                }
            )
            row["sample_batches"] += sum(e["event"] == "checker_sample_batch" for e in es)
            before = enters[0].get("input_ref")
            after = ends[0].get("adopted_output_ref") if ends else None
            changed = bool(before and after and before["sha256"] != after["sha256"])
            row["deterministic_changed_invocations"] += bool(changed and not repair)
            if after:
                row["parsed_outputs_not_adopted"] += sum(
                    e["event"] == "checker_parse"
                    and e.get("accepted") is True
                    and isinstance(e.get("output_ref"), dict)
                    and e["output_ref"].get("sha256") != after["sha256"]
                    for e in es
                )
            if not repair and not changed:
                row["no_observed_repair_or_change"] += 1
                continue
            row["observable_effect_events"] += 1
            b = score(i, before, ident + ":input")
            a = (
                score(i, after, ident + ":adopted")
                if after
                else {"strict": None, "reference": None}
            )
            effect = {
                "qid": i,
                "checker": name,
                "checker_invocation_id": ident,
                "input_ref": before,
                "adopted_output_ref": after,
                "repair_trigger_events": repair,
                "error": bool(errors),
                "before": b,
                "after": a,
            }
            for mode in ("strict", "reference"):
                label = classify(b[mode], a[mode])
                row[mode][label] += 1
                effect[mode + "_effect"] = label
            effects.append(effect)
    observed = [
        r
        for r in labels
        if isinstance(r.get("stages", {}).get("Generation", {}).get("candidates"), list)
        and not r["stages"]["Generation"].get("missing")
    ]
    finalslots = sum(len(r["stages"]["Generation"]["candidates"]) for r in observed)
    gen = {
        "inventory_sha256": inventory_sha,
        "covered_cases": covered,
        "observed_event_cases": len(seen),
        "target_slots": args.target_slots,
        "first_full_cases": initial_full,
        "final_full_cases": sum(
            len(r["stages"]["Generation"]["candidates"]) == args.target_slots for r in observed
        ),
        "retry_filled_cases": filled,
        "cases_with_any_slot_recovered": any_recovered,
        "initial_successful_slots": initial_slots,
        "internally_recovered_slots": sum(n for a, n in slot_success.items() if int(a) > 1),
        "final_successful_slots": finalslots,
        "success_attempt_distribution": dict(slot_success),
        "attempt_events": generator_calls,
        "internal_retry_attempt_events": retry_calls,
        "logical_calls": len(model_call_ids) if model_call_ids or not generator_calls else None,
        "internal_retry_calls": len(retry_model_ids)
        if model_call_ids or not generator_calls
        else None,
        "whole_case_retries": None,
        "sdk_physical_attempts": None,
        "coverage_note": "Initial/retry counts only exact mapped covered_cases; final slot counts from all supplied final candidate labels. Historical whole-case and SDK counts need separate registry/gate audit.",
    }
    checks = {
        "inventory_sha256": inventory_sha,
        "per_checker": checker,
        "coverage": {
            "event_cases": len(seen),
            "inventory_cases": len(frozen),
            "issues": len(issues),
        },
        "count_unit": "one checker invocation input to actual adopted output; multiple parse/model calls are not multiple trigger effects",
        "condition_note": "No dedicated condition-trigger/skip classification in v1. triggered/not_triggered/skipped remain null; observed repair invocations and deterministic changed outputs are explicit subsets.",
    }
    return {
        "generator-summary.json": gen,
        "checker-summary.json": checks,
        "coverage.json": {
            "inventory_sha256": inventory_sha,
            "unobserved_event_qids": sorted(set(frozen) - seen),
            "issues": issues,
            "pending_score_count": len(pending),
        },
        "pending-score-requests.jsonl": list(pending.values()),
        "checker-effects.safe.jsonl": effects,
        "generator-cases.safe.jsonl": generator_case_rows,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("inventory", "dataset", "labels", "events-manifest", "output"):
        p.add_argument("--" + key, required=True)
    p.add_argument("--score-map", action="append", default=[])
    p.add_argument("--target-slots", type=int, required=True)
    args = p.parse_args()
    result = aggregate(args)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out, 0o700)
    for name, data in result.items():
        raw = (
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in data)
            if name.endswith(".jsonl")
            else json.dumps(data, ensure_ascii=False, indent=2)
        )
        with os.fdopen(os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(raw)
        os.chmod(out / name, 0o600)
    verification = {
        "inputs": {
            k: {
                "path": str(Path(getattr(args, k)).resolve()),
                "sha256": digest(getattr(args, k)),
            }
            for k in ("inventory", "dataset", "labels", "events_manifest")
        },
        "scores": [{"path": str(Path(x).resolve()), "sha256": digest(x)} for x in args.score_map],
        "outputs": {name: digest(out / name) for name in result},
        "no_sql_blob_read_or_execution": True,
        "no_model_calls": True,
    }
    (out / "verification.json").write_text(json.dumps(verification, indent=2))
    os.chmod(out / "verification.json", 0o600)
    print(
        json.dumps(
            {
                "output": str(out.resolve()),
                "pending_scores": result["coverage.json"]["pending_score_count"],
                "coverage_issues": len(result["coverage.json"]["issues"]),
            }
        )
    )


if __name__ == "__main__":
    main()
