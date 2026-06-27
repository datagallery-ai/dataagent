#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""训练集向量化缓存构建器 — Few-Shot 离线预处理

从训练集原始 JSON（如 BIRD train.json）批量计算 SentenceTransformer embedding，
保存为缓存文件，供 step1h / generate_from_cache.py 复用，避免重复计算训练集 embedding。

产出文件（写入 config.FEW_SHOT_CACHE_DIR，默认 nl2sql/data/few_shot_data/）：
  train_embeddings.npy   训练集问题的 embedding 向量, shape (N, dim)
  train_cache.json       训练集元数据（model_name / train_items / created_at / num_items）

用法:
    # 默认：读 data/train/train.json，模型/输出目录取自 config
    python -m nl2sql.few_shot.build_train_cache

    # 自定义训练集与设备
    python -m nl2sql.few_shot.build_train_cache --train-json data/train/train.json --device cuda
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

try:
    from .. import config
except ImportError:  # 兼容脚本直跑
    import config  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---- 默认配置（取自 config，缺省时回退合理默认值） ----
DEFAULT_MODEL = getattr(config, "FEW_SHOT_SELECT_MODEL", "sentence-transformers/all-mpnet-base-v2")
DEFAULT_OUTPUT_DIR = Path(getattr(config, "FEW_SHOT_CACHE_DIR", config.NL2SQL_DIR / "data" / "few_shot_data"))
DEFAULT_TRAIN_JSON = config.NL2SQL_DIR / "data" / "train" / "train.json"
DEFAULT_DEVICE = getattr(config, "FEW_SHOT_DEVICE", "cpu")


def load_train_data(train_json: Path) -> List[Dict[str, Any]]:
    """加载训练集原始 JSON（每项需含 question / db_id / SQL|query）。"""
    train_json = Path(train_json)
    if not train_json.exists():
        raise FileNotFoundError(f"Train JSON not found: {train_json}")
    logger.info("Loading train data: %s", train_json)
    with train_json.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Train JSON should be a list: {train_json}")
    logger.info("  Loaded %d train items", len(data))
    return data


def build_train_cache(
    train_data: List[Dict[str, Any]],
    output_dir: Path,
    model_name: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
    batch_size: int = 64,
) -> Dict[str, Any]:
    """计算训练集 embedding 并保存缓存文件。

    Args:
        train_data:  训练数据列表，每项含 {question, db_id, SQL|query}
        output_dir:  缓存输出目录（写 train_embeddings.npy + train_cache.json）
        model_name:  SentenceTransformer 模型名
        device:      计算设备 (cpu / cuda)
        batch_size:  encode 批大小

    Returns:
        cache_meta 字典（已写入 train_cache.json 的内容）
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_questions = [str(d.get("question") or "") for d in train_data]

    # ---- Phase 1: 训练集 batch embedding ----
    logger.info("=" * 60)
    logger.info("Phase 1/2: Train-side Embedding (%d questions)", len(train_questions))
    logger.info("=" * 60)
    logger.info("  model=%s device=%s", model_name, device)

    if not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    from sentence_transformers import SentenceTransformer

    bert_model = SentenceTransformer(model_name, device=device)
    train_embeddings = bert_model.encode(
        train_questions,
        batch_size=batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
    )
    logger.info("  train_embeddings shape: %s", train_embeddings.shape)

    # ---- Phase 2: 保存缓存 ----
    logger.info("=" * 60)
    logger.info("Phase 2/2: Save Train Cache → %s/", output_dir)
    logger.info("=" * 60)

    emb_path = output_dir / "train_embeddings.npy"
    meta_path = output_dir / "train_cache.json"

    np.save(emb_path, train_embeddings)

    cache_meta = {
        "model_name": model_name,
        "train_items": [
            {
                "question": str(d.get("question") or ""),
                "SQL": str(d.get("SQL") or d.get("query") or ""),
                "db_id": str(d.get("db_id") or ""),
            }
            for d in train_data
        ],
        "created_at": datetime.now().isoformat(),
        "num_items": len(train_data),
    }
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(cache_meta, f, ensure_ascii=False)

    logger.info("  train_embeddings.npy: shape=%s", train_embeddings.shape)
    logger.info("  train_cache.json: %d items", len(train_data))
    return cache_meta


def main() -> None:
    parser = argparse.ArgumentParser(
        description="构建训练集向量化缓存（train_embeddings.npy + train_cache.json）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
    python -m nl2sql.few_shot.build_train_cache
    python -m nl2sql.few_shot.build_train_cache --train-json data/train/train.json --device cuda
""",
    )
    parser.add_argument(
        "--train-json", type=Path, default=DEFAULT_TRAIN_JSON,
        help=f"训练集 JSON 路径 (默认: {DEFAULT_TRAIN_JSON})",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"缓存输出目录 (默认: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--model", type=str, default=DEFAULT_MODEL,
        help=f"SentenceTransformer 模型名 (默认: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--device", type=str, default=DEFAULT_DEVICE, choices=["cpu", "cuda"],
        help=f"计算设备 (默认: {DEFAULT_DEVICE})",
    )
    parser.add_argument(
        "--batch-size", type=int, default=64,
        help="encode 批大小 (默认: 64)",
    )

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Train Cache Builder")
    logger.info("=" * 60)
    logger.info("Train data:  %s", args.train_json)
    logger.info("Output dir:  %s", args.output_dir)
    logger.info("Model:       %s", args.model)
    logger.info("Device:      %s", args.device)
    logger.info("=" * 60)

    start_time = datetime.now()

    train_data = load_train_data(args.train_json)
    build_train_cache(
        train_data=train_data,
        output_dir=args.output_dir,
        model_name=args.model,
        device=args.device,
        batch_size=args.batch_size,
    )

    elapsed = datetime.now() - start_time
    logger.info("Done! Total time: %s", elapsed)


if __name__ == "__main__":
    main()
