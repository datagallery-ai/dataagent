"""Score candidates archived by the BIRD workflow."""

import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from evaluate_sql import _execute, _projection
from evaluate_sql import compare as compare


def sha(x):
    return hashlib.sha256(x.encode()).hexdigest()


def score_case(c, gold, dbs):
    qid = int(c["qid"])
    q = gold[qid]
    db = dbs / q["db_id"] / (q["db_id"] + ".sqlite")
    out = {
        "qid": qid,
        "db": q["db_id"],
        "normal": c["normal"],
        "source_stats": c["source_stats"],
        "stats_sha256": c["stats_sha256"],
        "strict_top1": c["strict_correct"],
        "reference_top1": c["extra_columns_correct"],
        "stages": {},
    }
    if (
        hashlib.sha256(pathlib.Path(c["source_stats"]).read_bytes()).hexdigest()
        != c["stats_sha256"]
    ):
        raise ValueError(f"stats changed during scoring for {qid}")
    root = pathlib.Path(c["source_case_dir"]) / "nl2sql_context_dump"
    pools = {}
    for stage, name, field in [
        ("Generation", "002-generator.json", "generation_results"),
        ("AfterValidation", "003-bird-validator.json", "execution_results"),
    ]:
        p = root / name
        if not p.exists():
            out["stages"][stage] = {"missing": True, "candidates": []}
            continue
        raw = p.read_bytes()
        out.setdefault("artifact_sha256", {})[stage] = hashlib.sha256(raw).hexdigest()
        record = json.loads(raw)
        if record["case_id"] != c["case_id"]:
            raise ValueError(f"candidate state belongs to another case: {name}")
        pools[stage] = record["state"][field]
    try:
        gr, gw, gt = _execute(db, q["SQL"], 30)
        gs = set(map(tuple, gr))
        out["gold"] = {"status": "ok", "elapsed": gt, "sha256": sha(q["SQL"])}
    except Exception as e:
        out["gold"] = {"status": type(e).__name__, "sha256": sha(q["SQL"])}
        gs = None
    cache = {}
    # Reuse full captured successful execution result, never truncated previews.
    for z in pools.get("AfterValidation", []):
        if (
            z.get("total_known") is True
            and z.get("truncated") is False
            and not z.get("error")
            and isinstance(z.get("rows"), list)
        ):
            rr = z["rows"]
            cache[sha(z.get("sql", ""))] = (
                rr,
                len(z.get("columns") or (rr[0] if rr else [])),
                None,
                "captured",
            )
    for stage, pool in pools.items():
        scores = []
        groups = {}
        for index, z in enumerate(pool):
            sql = z.get("sql") or ""
            h = sha(sql)
            s = {
                "index": index,
                "id": z.get("id"),
                "strategy": z.get("strategy"),
                "sql_sha256": h,
                "sql_length": len(sql),
                "strict": None,
                "reference": None,
            }
            if h not in cache:
                try:
                    rr, w, t = _execute(db, sql, 30)
                    cache[h] = (rr, w, None, "readonly")
                except Exception as e:
                    cache[h] = (None, None, type(e).__name__, "readonly")
            rr, w, err, origin = cache[h]
            s.update(execution_error=err, execution_source=origin)
            if rr is not None:
                aset = frozenset(map(tuple, rr))
                groups.setdefault(aset, []).append(index)
                if gs is not None:
                    s["strict"] = set(aset) == gs
                    try:
                        s["reference"] = s["strict"] or (
                            w > gw and _projection(gs, set(aset), gw, w, 100000, 5) is not None
                        )
                    except Exception as e:
                        s["comparison_error"] = type(e).__name__
            elif err != "TimeoutError" and gs is not None:
                s["strict"] = False
                s["reference"] = False
            scores.append(s)
        group_list = list(groups.values())
        maxn = max(map(len, group_list), default=0)
        ties = [x for x in group_list if len(x) == maxn]
        chosen = min(ties[0], key=lambda i: scores[i]["sql_length"]) if ties else None
        out["stages"][stage] = {
            "candidates": scores,
            "vote_groups": group_list,
            "max_votes": maxn,
            "tied_max_groups": ties,
            "vote_index": chosen,
            "vote_strict": scores[chosen]["strict"] if chosen is not None else None,
            "vote_reference": scores[chosen]["reference"] if chosen is not None else None,
            "tie_oracle_strict": any(scores[i]["strict"] is True for g in ties for i in g)
            if ties
            else None,
            "tie_oracle_reference": any(scores[i]["reference"] is True for g in ties for i in g)
            if ties
            else None,
        }
    return out
