from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

try:
    from ... import config
except ImportError:
    import config

from ...common.atomic_io import atomic_write_json
from ..models.step1_models import StepResult
from .base import StepBuilder

logger = logging.getLogger(__name__)


class Step1hFewShotExamplesBuilder(StepBuilder):
    """few-shot 示例生成 builder（题目级断点）。

    question_id 字段在 dev 数据中一定存在，无 fallback；缺失直接报错。
    本步骤不调 LLM。
    输出：FEW_SHOT_PATH（workspace/few_shot/few_shot_examples.json）。
    """

    step_name = "step1h_build_few_shot_examples"

    def _run_impl(self) -> StepResult:
        # ---- 前置检查 ----
        dev_json_path = Path(config.DEV_JSON)
        if not dev_json_path.exists():
            raise FileNotFoundError(f"Dev JSON not found: {dev_json_path}")
        cache_dir = Path(config.FEW_SHOT_CACHE_DIR)
        if not cache_dir.exists():
            raise FileNotFoundError(
                f"Few-shot cache not found: {cache_dir}; "
                "place train_embeddings.npy + train_cache.json under this dir first"
            )
        emb_path = cache_dir / "train_embeddings.npy"
        meta_path = cache_dir / "train_cache.json"
        if not emb_path.exists() or not meta_path.exists():
            raise FileNotFoundError(
                f"Few-shot cache files missing under {cache_dir}: "
                f"expected train_embeddings.npy + train_cache.json"
            )

        # ---- Phase A: 加载训练缓存 + dev + 一次性 encode ----
        from ...few_shot.generate_from_cache import (
            load_train_cache,
            encode_dev_questions,
            knn_topk_for_question,
        )

        train_embeddings, cache_meta = load_train_cache(cache_dir)
        train_items = cache_meta["train_items"]
        model_name = cache_meta["model_name"]

        with dev_json_path.open("r", encoding="utf-8") as fp:
            dev_data: List[Dict[str, Any]] = json.load(fp)
        if not isinstance(dev_data, list):
            raise ValueError(f"Dev JSON should be a list: {dev_json_path}")

        # 每条 dev 题目必须含 question_id，缺失直接报错暴露数据问题
        for idx, item in enumerate(dev_data):
            if "question_id" not in item or item.get("question_id") is None:
                raise KeyError(
                    f"dev item missing required field 'question_id' at index {idx}: "
                    f"{json.dumps(item, ensure_ascii=False)[:200]}"
                )

        device = str(config.FEW_SHOT_DEVICE)
        num_examples = int(config.FEW_SHOT_NUM_EXAMPLES)

        completed = set(self.progress.completed_keys()) if not self.context.force_rerun else set()

        processed_count = 0
        skipped_count = 0
        outputs: List[str] = []

        # 先扫一遍：哪些题目还需要处理 → 只对这些题目调用 encode（避免无谓的整体 encode）
        pending_indices: List[int] = []
        replay_indices: List[int] = []  # staging 已存在但未登记
        for idx, item in enumerate(dev_data):
            qid = str(item.get("question_id"))
            staging_path = self.context.artifacts.step1h_question_staging_path(qid)

            if qid in completed:
                skipped_count += 1
                continue
            if not self.context.force_rerun and staging_path.exists():
                replay_indices.append(idx)
                continue
            pending_indices.append(idx)

        # 回放：staging 存在但未登记 → 补登记
        for idx in replay_indices:
            qid = str(dev_data[idx].get("question_id"))
            staging_path = self.context.artifacts.step1h_question_staging_path(qid)
            self.progress.add_completed_key(qid)
            completed.add(qid)
            outputs.append(str(staging_path))
            skipped_count += 1

        # ---- Phase B: 逐题 encode + KNN + 写 staging ----
        if pending_indices:
            pending_items = [dev_data[i] for i in pending_indices]
            logger.info(
                "step1h: encoding %d/%d dev questions (skipped=%d, replay=%d)",
                len(pending_items), len(dev_data), len(completed) - len(replay_indices), len(replay_indices),
            )
            pending_embeddings = encode_dev_questions(
                pending_items, model_name=model_name, device=device
            )

            for offset, idx in enumerate(pending_indices):
                item = dev_data[idx]
                qid = str(item.get("question_id"))
                target_db_id = str(item.get("db_id") or "")
                target_question = str(item.get("question") or "")

                examples = knn_topk_for_question(
                    qvec=pending_embeddings[offset],
                    train_embeddings=train_embeddings,
                    train_items=train_items,
                    k=num_examples,
                    cross_domain=False,
                    db_id=target_db_id,
                    target_question=target_question,
                )

                staging_path = self.context.artifacts.step1h_question_staging_path(qid)
                # 先写产物，再标记 state
                atomic_write_json(staging_path, {"question_id": qid, "examples": examples})
                outputs.append(str(staging_path))

                self.progress.add_completed_key(qid)
                completed.add(qid)
                processed_count += 1

        # ---- Phase C: 全部 dev 都登记后聚合 staging → atomic 写最终输出 ----
        all_present = all(str(item.get("question_id")) in completed for item in dev_data)
        if all_present:
            aggregated: Dict[str, List[Dict[str, str]]] = {}
            for item in dev_data:
                qid = str(item.get("question_id"))
                staging_path = self.context.artifacts.step1h_question_staging_path(qid)
                if not staging_path.exists():
                    raise FileNotFoundError(
                        f"step1h: staging missing for completed question_id={qid} at {staging_path}"
                    )
                with staging_path.open("r", encoding="utf-8") as fp:
                    payload = json.load(fp)
                aggregated[qid] = payload.get("examples") or []

            output_path = self.context.artifacts.few_shot_output_path
            atomic_write_json(output_path, aggregated)
            outputs.append(str(output_path))
        else:
            logger.warning(
                "step1h: not all dev questions completed yet; few_shot_examples.json not aggregated this run"
            )

        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=outputs,
        )
