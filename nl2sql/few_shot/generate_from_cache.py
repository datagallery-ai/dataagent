"""基于训练集向量化缓存的 Few-Shot 示例快速生成器

从缓存目录加载预计算的训练集 embedding + 元数据，结合 dev 题目集快速生成
few_shot_examples.json，无需重新计算训练集 embedding。

缓存文件由 build_train_cache.py 生成：
  train_embeddings.npy   训练集问题的 embedding 向量 (N, dim)
  train_cache.json       训练集元数据 (question, SQL, db_id)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


def load_train_cache(cache_dir: Path) -> Tuple[np.ndarray, Dict[str, Any]]:
    """加载训练集向量化缓存。

    Args:
        cache_dir: 缓存目录路径

    Returns:
        (train_embeddings, cache_meta) 元组
    """
    cache_dir = Path(cache_dir)
    emb_path = cache_dir / "train_embeddings.npy"
    meta_path = cache_dir / "train_cache.json"

    if not emb_path.exists() or not meta_path.exists():
        raise FileNotFoundError(
            f"Cache not found in {cache_dir}/\n"
            f"  Expected: train_embeddings.npy + train_cache.json\n"
            f"  Run: python -m nl2sql.few_shot.build_train_cache"
        )

    logger.info("Loading train cache from %s/", cache_dir)

    train_embeddings = np.load(emb_path)
    with open(meta_path, "r", encoding="utf-8") as f:
        cache = json.load(f)

    logger.info("  train_embeddings: shape=%s", train_embeddings.shape)
    logger.info("  train items: %s", cache.get("num_items"))
    logger.info("  model: %s", cache.get("model_name"))
    logger.info("  created: %s", cache.get("created_at"))

    return train_embeddings, cache


def encode_dev_questions(
    dev_items: Sequence[Dict[str, Any]],
    model_name: str,
    device: str = "cpu",
    batch_size: int = 64,
) -> np.ndarray:
    """一次性 encode dev 全部题目的 question 文本。

    Args:
        dev_items:  dev 题目列表（每项需含 'question' 字段）
        model_name: SentenceTransformer 模型名（应与训练缓存一致）
        device:     设备（cpu/cuda）
        batch_size: encode 批大小

    Returns:
        np.ndarray, shape = (len(dev_items), dim)
    """
    if not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    from sentence_transformers import SentenceTransformer

    logger.info("Loading SentenceTransformer: %s", model_name)
    bert_model = SentenceTransformer(model_name, device=device)

    questions = [str(item.get("question") or "") for item in dev_items]
    if not questions:
        return np.empty((0, 1), dtype=np.float32)
    embeddings = bert_model.encode(
        questions,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return embeddings


def knn_topk_for_question(
    qvec: np.ndarray,
    train_embeddings: np.ndarray,
    train_items: Sequence[Dict[str, Any]],
    k: int = 5,
    cross_domain: bool = False,
    db_id: str = "",
    target_question: str = "",
) -> List[Dict[str, str]]:
    """对单个 dev 题目向量做 KNN，返回 top-k few-shot 示例。

    Args:
        qvec:              dev 题目的 embedding，shape=(dim,) 或 (1, dim)
        train_embeddings:  训练集 embedding 矩阵，shape=(N, dim)
        train_items:       训练集元数据列表（每项含 question / SQL / db_id）
        k:                 返回示例数
        cross_domain:      True 时排除同一 db_id 的训练样本
        db_id:             dev 题目所在 db_id（用于跨域过滤）
        target_question:   dev 题目原文（用于精确去重，避免示例 == 自身）

    Returns:
        [{'question': ..., 'sql': ...}, ...]，长度 ≤ k
    """
    # 纯 numpy 向量化实现：按欧氏距离平方升序排名。
    # 使用 stable 排序，保证同距离时按原始索引顺序。
    query_vec = np.asarray(qvec, dtype=np.float64).reshape(-1)
    train_mat = np.asarray(train_embeddings, dtype=np.float64)
    if train_mat.ndim == 1:
        train_mat = train_mat.reshape(1, -1)

    deltas = train_mat - query_vec
    dist_sq = np.einsum("ij,ij->i", deltas, deltas)
    ranked_indices = np.argsort(dist_sq, kind="stable")

    target_db = str(db_id)
    examples: List[Dict[str, str]] = []
    for pos in ranked_indices:
        item = train_items[int(pos)]
        train_q = str(item.get("question") or "")
        if target_question and train_q == target_question:
            continue
        if cross_domain and db_id and str(item.get("db_id") or "") == target_db:
            continue
        examples.append({
            "question": train_q,
            "sql": str(item.get("SQL") or item.get("query") or ""),
        })
        if len(examples) >= int(k):
            break
    return examples


def generate_few_shots_from_cache(
    cache_dir: Path,
    dev_json_path: str,
    num_examples: int = 5,
    cross_domain: bool = False,
    device: str = "cpu",
) -> Dict[str, List[Dict[str, str]]]:
    """基于缓存生成 few-shot 示例（薄封装，复用上面三个函数）。

    Args:
        cache_dir:      训练集缓存目录 (含 train_embeddings.npy + train_cache.json)
        dev_json_path:  Dev 题目集 JSON 路径
        num_examples:   每题返回的 few-shot 示例数
        cross_domain:   是否跨域选择 (排除同一数据库的样本)
        device:         SentenceTransformer 计算设备

    Returns:
        {question_id: [{question, sql}, ...]}
    """
    # ---- Phase 1: 加载训练缓存 ----
    logger.info("=" * 60)
    logger.info("Phase 1/2: Load Train Cache (skip embedding computation)")
    logger.info("=" * 60)
    train_embeddings, cache = load_train_cache(Path(cache_dir))
    train_items = cache["train_items"]
    model_name = cache["model_name"]

    # ---- 加载 dev 数据 ----
    logger.info("Loading dev data: %s", dev_json_path)
    with open(dev_json_path, "r", encoding="utf-8") as f:
        dev_data = json.load(f)
    logger.info("  Loaded %d dev items", len(dev_data))

    # ---- 一次性 encode 全部 dev ----
    dev_embeddings = encode_dev_questions(dev_data, model_name=model_name, device=device)

    # ---- Phase 2: 逐题匹配 ----
    logger.info("=" * 60)
    logger.info("Phase 2/2: Dev Few-shot Generation (%d questions)", len(dev_data))
    logger.info("=" * 60)
    logger.info("  Embedding distance → top-%d examples", num_examples)

    few_shot_examples: Dict[str, List[Dict[str, str]]] = {}
    total_dev = len(dev_data)

    for i, item in enumerate(dev_data):
        question_id = str(item.get("question_id", item.get("id", i)))
        target_db_id = str(item.get("db_id") or "")
        target_question = str(item.get("question") or "")

        if (i + 1) % 100 == 0 or i == 0:
            logger.info(
                "[Step 2] %d/%d  db=%s  q=\"%s\"",
                i + 1, total_dev, target_db_id, target_question,
            )

        examples = knn_topk_for_question(
            qvec=dev_embeddings[i],
            train_embeddings=train_embeddings,
            train_items=train_items,
            k=num_examples,
            cross_domain=cross_domain,
            db_id=target_db_id,
            target_question=target_question,
        )
        few_shot_examples[question_id] = examples

    return few_shot_examples


# 兼容旧入口名
load_cache = load_train_cache
