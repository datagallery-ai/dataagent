"""Few-shot 示例选择模块

基于 SentenceTransformer 问题向量欧氏距离的简单相似匹配，
生成 few_shot_examples.json。
"""
from .generate_from_cache import (
    load_train_cache,
    encode_dev_questions,
    knn_topk_for_question,
    generate_few_shots_from_cache,
)
from .build_train_cache import build_train_cache, load_train_data

__all__ = [
    'load_train_cache',
    'encode_dev_questions',
    'knn_topk_for_question',
    'generate_few_shots_from_cache',
    'build_train_cache',
    'load_train_data',
]
