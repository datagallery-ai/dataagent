"""Step4a SFT 选择器流程。"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from .. import config
from ..common.atomic_io import atomic_write_json
from ..common.data_loader import load_dev_questions
from ..common.runner_utils import ErrorManager
from .scorer import ScoreRequest, ScoreResult, YesNoVLLMScorer


logger = logging.getLogger("step4a_sft")

PROMPT_TEMPLATE_PATH = config.NL2SQL_DIR / "sft_selector" / "user_prompt.md"
WORK_DIR = config.WORKSPACE_ROOT / "sft_selector"
OUTPUT_DIR = config.NL2SQL_DIR / "output"
QUESTION_BATCH_SIZE = 16
TENSOR_PARALLEL_SIZE = 1
CONFIDENCE_THRESHOLD = 0.74

VLLM_DEFAULTS = {
    "max_model_len": 4096,
    "dtype": "bfloat16",
    "gpu_memory_utilization": 0.85,
    "topk_logprobs": 20,
    "chunk_size": 2048,
    "enforce_eager": True,
    "qwen3_empty_think_prefix": True,
    "trust_remote_code": True,
}


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    """原子写入 JSON，复用 common.atomic_io 带 retry 的实现。

    实现上与原本的 ``path.with_suffix('.tmp') + replace`` 一致，但：
      - tmp 名含 uuid，并发场景不冲突
      - Windows 上 os.replace 撞 PermissionError [WinError 5] 时自动重试。
    """
    atomic_write_json(path, data)


def result_path(work_dir: Path, qid: int) -> Path:
    return work_dir / f"q_{qid:04d}_sft_selected.json"


def load_selector_records(source_run_id: str) -> Dict[int, dict]:
    selector_dir = config.GENERATION_OUTPUT_DIR / source_run_id / "selector"
    if not selector_dir.exists():
        raise FileNotFoundError(f"Selector directory not found: {selector_dir}")

    records: Dict[int, dict] = {}
    for path in sorted(selector_dir.glob("q_*_selected.json")):
        try:
            records[int(path.stem.split("_")[1])] = read_json(path)
        except (IndexError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Skip invalid selector file %s: %s", path, exc)
    logger.info("Loaded %d selector records from %s", len(records), selector_dir)
    return records


def completed_qids(work_dir: Path) -> set[int]:
    done = set()
    for path in work_dir.glob("q_*_sft_selected.json"):
        try:
            done.add(int(path.stem.split("_")[1]))
        except (IndexError, ValueError):
            pass
    return done


def selector_confidence(record: dict) -> float:
    return float(record.get("confidence", 0.0) or 0.0)


def is_single(record: dict) -> bool:
    return abs(selector_confidence(record) - 1.0) < 1e-9


def normalize_sql(sql: str) -> str:
    return " ".join((sql or "").strip().split())


def candidates(record: dict) -> List[dict]:
    raw = record.get("sql_after_validation") or record.get("sql_candidates_after_revision") or record.get("sql_candidates") or []
    out: List[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(raw):
        sql = (item.get("sql") if isinstance(item, dict) else str(item or "")).strip()
        key = normalize_sql(sql)
        if sql and key not in seen:
            seen.add(key)
            out.append({"candidate_idx": idx, "sql": sql})

    if out:
        return out

    for sql in (record.get("full_review_sql"), record.get("selector_output_sql"), record.get("top1_sql")):
        key = normalize_sql(sql or "")
        if sql and key not in seen:
            seen.add(key)
            out.append({"candidate_idx": len(out), "sql": sql})
    return out


def top1_sql(record: dict) -> str:
    cands = candidates(record)
    return record.get("top1_sql") or record.get("selector_output_sql") or record.get("full_review_sql") or (cands[0]["sql"] if cands else "")


def score_requests(qid: int, question: dict, record: dict) -> List[ScoreRequest]:
    return [
        ScoreRequest(qid, int(c["candidate_idx"]), question.get("question", ""), question.get("evidence", ""), c["sql"])
        for c in candidates(record)
    ]


def pick_sft(scores: List[ScoreResult]) -> Optional[ScoreResult]:
    return max(scores, key=lambda s: (s.yes_probability, -s.candidate_idx)) if scores else None


def build_result(qid: int, question: dict, source_run_id: str, record: dict, scores: List[ScoreResult], status: str) -> dict:
    record_top1 = top1_sql(record)
    winner = pick_sft(scores)
    selected_sql = winner.sql if winner else record_top1
    return {
        "question_id": qid,
        "db_id": record.get("db_id") or question["db_id"],
        "source_run_id": source_run_id,
        "status": status,
        "selector_confidence": selector_confidence(record),
        "selector_top1_sql": record_top1,
        "sft_selected_sql": selected_sql,
        "full_review_sql": selected_sql,
        "sft_selected_score": winner.to_dict() if winner else None,
        "candidate_scores": [s.to_dict() for s in scores],
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def final_sql(record: dict) -> dict:
    conf = float(record.get("selector_confidence", 0.0) or 0.0)
    if abs(conf - 1.0) < 1e-9:
        stage = "single_cluster"
        sql = record.get("selector_top1_sql") or ""
    elif conf > CONFIDENCE_THRESHOLD:
        stage = "max_cluster_threshold"
        sql = record.get("selector_top1_sql") or ""
    else:
        stage = "sft_s1"
        sql = record.get("sft_selected_sql") or record.get("selector_top1_sql") or ""
    return {"sql": sql, "stage": stage, "selector_confidence": conf}


def create_scorer(model_path: str) -> YesNoVLLMScorer:
    if not model_path:
        raise ValueError("SFT selector model path is empty")
    logger.info("Loading SFT selector model: %s", model_path)
    return YesNoVLLMScorer(
        model_path=model_path,
        prompt_template_path=PROMPT_TEMPLATE_PATH,
        tensor_parallel_size=TENSOR_PARALLEL_SIZE,
        **VLLM_DEFAULTS,
    )


def _write_qid_result(
    qid: int,
    qid_scores: List[ScoreResult],
    expected_count: int,
    dev_questions: Dict[int, dict],
    selector_records: Dict[int, dict],
    source_run_id: str,
    work_dir: Path,
    errors: ErrorManager,
) -> bool:
    """将单题得分写入成果文件。返回是否成功。"""
    if len(qid_scores) != expected_count:
        errors.add(qid, f"Expected {expected_count} scores, got {len(qid_scores)}")
        return False
    write_json(
        result_path(work_dir, qid),
        build_result(qid, dev_questions[qid], source_run_id, selector_records[qid], qid_scores, "sft_s1"),
    )
    errors.remove(qid)
    return True


def _score_qids_individually(
    scorer: YesNoVLLMScorer,
    qids: List[int],
    expected: Dict[int, int],
    qid_to_requests: Dict[int, List[ScoreRequest]],
    dev_questions: Dict[int, dict],
    selector_records: Dict[int, dict],
    source_run_id: str,
    work_dir: Path,
    errors: ErrorManager,
) -> tuple[int, int]:
    """逐题调用 scorer，避免单题异常拖累整 batch。P3 单题兜底路径。"""
    ok = bad = 0
    for qid in qids:
        reqs = qid_to_requests.get(qid, [])
        if not reqs:
            errors.add(qid, "No SQL candidates for SFT scoring")
            bad += 1
            continue
        try:
            qid_scores = list(scorer.score(reqs))
        except Exception as exc:
            errors.add(qid, f"{type(exc).__name__}: {exc}")
            logger.warning("SFT per-qid fallback failed for q_%04d: %s", qid, exc)
            bad += 1
            continue
        if _write_qid_result(qid, qid_scores, expected.get(qid, 0), dev_questions, selector_records, source_run_id, work_dir, errors):
            ok += 1
        else:
            bad += 1
    return ok, bad


def score_and_write_batch(
    scorer: YesNoVLLMScorer,
    qids: List[int],
    dev_questions: Dict[int, dict],
    selector_records: Dict[int, dict],
    source_run_id: str,
    work_dir: Path,
    errors: ErrorManager,
) -> tuple[int, int]:
    qid_to_requests: Dict[int, List[ScoreRequest]] = {}
    expected: Dict[int, int] = {}
    for qid in qids:
        reqs = score_requests(qid, dev_questions[qid], selector_records[qid])
        if not reqs:
            errors.add(qid, "No SQL candidates for SFT scoring")
            continue
        qid_to_requests[qid] = reqs
        expected[qid] = len(reqs)

    valid_qids = list(qid_to_requests.keys())
    if not valid_qids:
        return 0, len(qids) - len(valid_qids)

    flat_requests: List[ScoreRequest] = []
    for qid in valid_qids:
        flat_requests.extend(qid_to_requests[qid])

    # 先试整 batch（性能最优），失败后退化为逐题 fallback
    try:
        scores = list(scorer.score(flat_requests))
    except Exception as exc:
        logger.warning(
            "SFT batch failed for %d questions, fallback to per-qid retry: %s",
            len(valid_qids), exc,
        )
        return _score_qids_individually(
            scorer, valid_qids, expected, qid_to_requests,
            dev_questions, selector_records, source_run_id, work_dir, errors,
        )

    by_qid: Dict[int, list] = defaultdict(list)
    for score in scores:
        by_qid[score.question_id].append(score)

    ok = bad = 0
    # 对未达期望计数的 qid 单独重试（避免 batch 内部少返回问题拖累）
    for qid in valid_qids:
        qid_scores = by_qid.get(qid, [])
        if len(qid_scores) != expected.get(qid, 0):
            # 单题重试一次
            try:
                retry_scores = list(scorer.score(qid_to_requests[qid]))
            except Exception as exc:
                errors.add(qid, f"Mismatch+RetryFailed: {type(exc).__name__}: {exc}")
                bad += 1
                continue
            if _write_qid_result(qid, retry_scores, expected.get(qid, 0),
                                 dev_questions, selector_records, source_run_id, work_dir, errors):
                ok += 1
            else:
                bad += 1
            continue
        if _write_qid_result(qid, qid_scores, expected.get(qid, 0),
                             dev_questions, selector_records, source_run_id, work_dir, errors):
            ok += 1
        else:
            bad += 1
    return ok, bad


def finalize_outputs(run_id: str, output_name: str, dev_questions: Optional[Dict[int, dict]] = None) -> dict:
    dev_questions = dev_questions or load_dev_questions(config.DEV_JSON)
    work_dir = WORK_DIR / run_id
    output_path = OUTPUT_DIR / output_name
    decisions_path = work_dir / "decisions.json"

    output: Dict[str, str] = {}
    decisions: Dict[str, dict] = {}
    missing = 0
    for qid in sorted(dev_questions):
        path = result_path(work_dir, qid)
        if not path.exists():
            output[str(qid)] = ""
            decisions[str(qid)] = {"stage": "missing_result"}
            missing += 1
            continue
        result = read_json(path)
        decision = final_sql(result)
        output[str(qid)] = decision["sql"]
        decisions[str(qid)] = {
            "stage": decision["stage"],
            "selector_confidence": decision["selector_confidence"],
            "sft_status": result.get("status"),
        }

    write_json(output_path, output)
    write_json(decisions_path, decisions)
    logger.info("Final output written: %s (%d questions, %d missing)", output_path, len(output), missing)
    return {"output_path": str(output_path), "decisions_path": str(decisions_path), "total": len(dev_questions), "missing": missing}


def parse_qid_list(raw: Optional[str]) -> Set[int]:
    """解析 ``--rerun-qids`` 参数，支持逗号/空格/分号分隔。空串返回空集。"""
    if not raw:
        return set()
    out: Set[int] = set()
    for tok in raw.replace(",", " ").replace(";", " ").split():
        try:
            out.add(int(tok))
        except ValueError:
            raise ValueError(f"--rerun-qids 含非法题号: {tok!r}")
    return out


def run_all_questions(
    *,
    run_id: str,
    source_run_id: str,
    model_path: str,
    output_name: str,
    max_retries: int,
    force_rerun: bool = False,
    finalize_only: bool = False,
    rerun_qids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    start = time.time()
    rerun_qids_set: Set[int] = set(rerun_qids or [])
    dev_questions = load_dev_questions(config.DEV_JSON)
    work_dir = WORK_DIR / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    errors = ErrorManager(work_dir / "errors.json")

    if finalize_only:
        summary = finalize_outputs(run_id, output_name, dev_questions)
        summary.update({"completed": len(completed_qids(work_dir)), "errors": errors.count()})
        return summary

    selector_records = load_selector_records(source_run_id)
    scorer: Optional[YesNoVLLMScorer] = None
    retries_used = 0

    for round_idx in range(max_retries + 1):
        if round_idx == 0:
            done = completed_qids(work_dir)
            all_sorted = sorted(dev_questions)
            if rerun_qids_set:
                # 单题重跑：仅处理指定 qid，忽略 done；清理该 qid 的历史错误
                valid = rerun_qids_set & set(all_sorted)
                invalid = sorted(rerun_qids_set - valid)
                if invalid:
                    logger.warning("--rerun-qids 中不在 dev.json 的题号已忽略: %s", invalid)
                qids = sorted(valid)
                for qid in qids:
                    errors.remove(qid)
                logger.info(
                    "[Round 0] Rerun-qids mode: %d to rerun (ignoring %d previously completed)",
                    len(qids), len(done & valid),
                )
            elif force_rerun:
                qids = list(all_sorted)
                logger.info("[Round 0] Force-rerun mode: %d questions (ignoring %d previously completed)",
                            len(qids), len(done))
            else:
                qids = [qid for qid in all_sorted if qid not in done]
                logger.info("[Round 0] resume: %d done, %d todo", len(done), len(qids))
        else:
            qids = errors.get_failed_ids()
            if not qids:
                break
            retries_used = round_idx
            logger.info("[Round %d] retrying %d failed questions", round_idx, len(qids))

        single_count = threshold_count = missing_count = 0
        sft_qids: List[int] = []
        for qid in qids:
            record = selector_records.get(qid)
            if record is None:
                missing_count += 1
                errors.add(qid, f"Missing selector record for q_{qid:04d}")
            elif is_single(record):
                write_json(
                    result_path(work_dir, qid),
                    build_result(qid, dev_questions[qid], source_run_id, record, [], "single_cluster"),
                )
                errors.remove(qid)
                single_count += 1
            elif selector_confidence(record) > CONFIDENCE_THRESHOLD:
                write_json(
                    result_path(work_dir, qid),
                    build_result(qid, dev_questions[qid], source_run_id, record, [], "max_cluster_threshold"),
                )
                errors.remove(qid)
                threshold_count += 1
            else:
                sft_qids.append(qid)

        sft_ok = sft_bad = 0
        if sft_qids:
            scorer = scorer or create_scorer(model_path)
            batches = [sft_qids[i : i + QUESTION_BATCH_SIZE] for i in range(0, len(sft_qids), QUESTION_BATCH_SIZE)]
            for idx, batch in enumerate(batches, start=1):
                logger.info("[Round %d] SFT batch %d/%d: %d questions", round_idx, idx, len(batches), len(batch))
                ok, bad = score_and_write_batch(scorer, batch, dev_questions, selector_records, source_run_id, work_dir, errors)
                sft_ok += ok
                sft_bad += bad

        logger.info(
            "[Round %d] single=%d threshold=%d sft=%d errors=%d missing=%d",
            round_idx,
            single_count,
            threshold_count,
            sft_ok,
            sft_bad,
            missing_count,
        )

    summary = {
        **finalize_outputs(run_id, output_name, dev_questions),
        "run_id": run_id,
        "source_run_id": source_run_id,
        "completed": len(completed_qids(work_dir)),
        "errors": errors.count(),
        "retries_used": retries_used,
        "elapsed_seconds": round(time.time() - start, 1),
    }
    write_json(work_dir / "summary.json", summary)
    logger.info("Step4a done: completed=%d errors=%d output=%s", summary["completed"], summary["errors"], summary["output_path"])
    return summary
