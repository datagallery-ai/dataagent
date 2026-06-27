from __future__ import annotations

import pickle
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
from datasketch import MinHash

try:
    from .. import config
except ImportError:
    import config

from ..preprocess.builders.step1d_join_graph import resolve_join_paths
from ..common.atomic_io import read_json
from ..preprocess.core.embedding import EmbeddingEncoder

_TOKEN_SPLIT_PATTERN = re.compile(r"[^a-zA-Z0-9_\u4e00-\u9fff]+")


class LocalArtifactClient:
    def __init__(self, workspace_root: str | Path | None = None, cache_dir: str | Path | None = None):
        self._workspace_root = Path(workspace_root) if workspace_root else Path(config.STEP1_PREPROCESS_WORKSPACE_DIR)
        self._cache_dir = Path(cache_dir) if cache_dir else Path(config.STEP1_CACHE_DIR)
        self._table_list_cache = read_json(self._cache_dir / "table_list_cache.json", default={})
        self._columns_info_cache = read_json(self._cache_dir / "table_columns_info_cache.json", default={})
        self._sample_values_cache = read_json(self._cache_dir / "columns_sample_values_cache.json", default={})
        self._encoder: EmbeddingEncoder | None = None
        self._column_store_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._value_desc_store_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._lsh_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._cache_lock = threading.Lock()                 # 仅保护下面几个 OrderedDict 的结构性读写
        self._db_locks: Dict[str, threading.Lock] = {}      # "{cache_name}:{db}" -> 该缓存类×库的加载锁
        self._encoder_lock = threading.Lock()               # 保护 encoder 单例初始化

    def get_table_list(self, database_name: str) -> List[Dict]:
        cached = self._table_list_cache.get(database_name)
        return cached if isinstance(cached, list) else []

    def get_table_columns_info(self, database_name: str, table_names: List[str]) -> Dict[str, List[Dict]]:
        db_cache = self._columns_info_cache.get(database_name)
        if not isinstance(db_cache, dict):
            return {table_name: [] for table_name in table_names}
        return {table_name: list(db_cache.get(table_name) or []) for table_name in table_names}

    def get_columns_sample_values(self, column_ids: List[str], sample_count: int = 3) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        for column_id in column_ids:
            values = self._sample_values_cache.get(column_id)
            if not isinstance(values, list):
                continue
            result[column_id] = [str(item) for item in values[:sample_count]]
        return result

    def get_table_relations_path(self, db_table1: str, db_table2: str, max_depth: int = 5) -> List[List[Dict]]:
        db_name = _extract_db_name(db_table1)
        other_db = _extract_db_name(db_table2)
        if not db_name or not other_db or db_name != other_db:
            return []
        payload = read_json(self._workspace_root / "join_relations" / f"{db_name}.json", default={})
        if not isinstance(payload, dict):
            return []
        return resolve_join_paths(payload, db_table1, db_table2, max_depth=max_depth)

    def semantic_search_columns(
        self,
        keywords: List[str],
        database_name: str,
        top_k: int = 5,
        vector_boost: float = None,
        text_boost: float = None,
        search_columns: bool = True,
        search_values: bool = False,
        batch_size: int = 5,
        timeout: int = None,
    ) -> List[Dict]:
        _ = batch_size
        _ = timeout
        store = self._load_column_store(database_name)
        metadata = store.get("metadata") or []
        normalized = store.get("normalized")
        if normalized is None or len(metadata) == 0:
            return [{kw: {"column_name_search": [], "column_value_match": []}} for kw in keywords]

        encoder = self._get_encoder()
        query_vectors = encoder.encode(keywords)
        eff_vector_boost = float(vector_boost if vector_boost is not None else config.COLUMN_SEMANTIC_MATCH_VECTOR_BOOST)
        eff_text_boost = float(text_boost if text_boost is not None else config.COLUMN_SEMANTIC_MATCH_TEXT_BOOST)
        value_match_map: Dict[str, Dict[str, Any]] = {}
        if search_values:
            for item in self.search_column_value_descriptions(keywords, database_name, top_k=top_k):
                if isinstance(item, dict):
                    value_match_map.update(item)

        results: List[Dict] = []
        for index, keyword in enumerate(keywords):
            item: Dict[str, Any] = {keyword: {}}
            if search_columns:
                query_normalized = _normalize_vector(query_vectors[index])
                vector_scores = normalized @ query_normalized
                ranked: List[tuple[str, float]] = []
                for row_index, meta in enumerate(metadata):
                    column_id = str(meta.get("column_id") or "").strip()
                    if not column_id:
                        continue
                    text = str(meta.get("text") or "")
                    hybrid_score = eff_vector_boost * float(vector_scores[row_index]) + eff_text_boost * _text_match_score(keyword, text)
                    ranked.append((column_id, hybrid_score))
                ranked.sort(key=lambda value: value[1], reverse=True)
                item[keyword]["column_name_search"] = [
                    {column_id: {"score": float(score)}}
                    for column_id, score in ranked[:max(int(top_k), 0)]
                ]
            else:
                item[keyword]["column_name_search"] = []
            if search_values:
                kw_payload = value_match_map.get(keyword, {})
                item[keyword]["column_value_match"] = list(kw_payload.get("column_value_match") or [])
            else:
                item[keyword]["column_value_match"] = []
            results.append(item)
        return results

    def search_column_value_descriptions(self, keywords: List[str], database_name: str, top_k: int = 3) -> List[Dict]:
        store = self._load_value_desc_store(database_name)
        metadata = store.get("metadata") or []
        normalized = store.get("normalized")
        if normalized is None or len(metadata) == 0:
            return [{kw: {"column_value_match": []}} for kw in keywords]

        encoder = self._get_encoder()
        query_vectors = encoder.encode(keywords)
        results: List[Dict] = []
        for index, keyword in enumerate(keywords):
            query_normalized = _normalize_vector(query_vectors[index])
            scores = normalized @ query_normalized
            ranked_indices = np.argsort(-scores)[:max(int(top_k), 0)]
            grouped: Dict[str, Dict[str, Any]] = {}
            for row_index in ranked_indices:
                meta = metadata[int(row_index)]
                column_id = str(meta.get("column_id") or "").strip()
                if not column_id:
                    continue
                if column_id not in grouped:
                    grouped[column_id] = {
                        "data_type": meta.get("column_type", ""),
                        "values": [],
                    }
                grouped[column_id]["values"].append(
                    {
                        "value": meta.get("value", ""),
                        "description": meta.get("description", ""),
                        "score": float(scores[int(row_index)]),
                    }
                )
            results.append(
                {
                    keyword: {
                        "column_value_match": [
                            {column_id: payload}
                            for column_id, payload in grouped.items()
                        ]
                    }
                }
            )
        return results

    def lsh_match(self, database: str, query: str, top_k: int = 3, threshold: float = 0.4) -> List[Dict]:
        payload = self._load_lsh_index(database)
        lsh = payload.get("lsh")
        minhashes = payload.get("minhashes") or {}
        records = payload.get("records") or {}
        meta = payload.get("meta") or {}
        params = meta.get("params") or {}
        if lsh is None:
            return []
        query_minhash = _create_minhash(
            query,
            num_perm=int(params.get("num_perm") or meta.get("num_perm") or config.LSH_NUM_PERM),
            k=int(params.get("k") or meta.get("k") or config.LSH_K),
        )
        matches: List[Dict] = []
        for key in lsh.query(query_minhash):
            stored = minhashes.get(key)
            if stored is None:
                continue
            score = float(query_minhash.jaccard(stored))
            if score < float(threshold):
                continue
            record = records.get(key) or {}
            if not record:
                record = _parse_lsh_key(key)
            matches.append(
                {
                    "matched_column": record.get("matched_column", ""),
                    "matched_value": record.get("matched_value", ""),
                    "match_type": "lsh",
                    "score": score,
                }
            )
        matches.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return matches[:max(int(top_k), 0)]

    def _get_encoder(self) -> EmbeddingEncoder:
        if self._encoder is not None:                       # fast path：单例已就绪，无锁返回
            return self._encoder
        with self._encoder_lock:                            # double-checked locking
            if self._encoder is None:
                self._encoder = EmbeddingEncoder(model_name=config.EMBEDDING_MODEL, device=config.EMBEDDING_DEVICE)
            return self._encoder

    def _get_db_lock(self, lock_key: str) -> threading.Lock:
        with self._cache_lock:
            lk = self._db_locks.get(lock_key)
            if lk is None:
                lk = threading.Lock()
                self._db_locks[lock_key] = lk
            return lk

    def _cached_load(self, cache: "OrderedDict[str, Any]", key: str, loader, cache_name: str) -> Any:
        # 1) fast path：命中直接返回（仅短暂持 _cache_lock 做 LRU touch）
        with self._cache_lock:
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
        # 2) per-(cache,db) 加载锁：同库同类串行加载、不同库/不同类可并行
        lock_key = f"{cache_name}:{key}"                     # 按"缓存类型+库"命名：避免同库跨类过度串行 & 杜绝重入自死锁
        with self._get_db_lock(lock_key):
            with self._cache_lock:                          # double-check：等锁期间可能已被别的线程加载
                if key in cache:
                    cache.move_to_end(key)
                    return cache[key]
            payload = loader()                              # 耗时加载在 db_lock 内、_cache_lock 外：
                                                            # 既保证同库只 load 一次，又不阻塞其他库的命中查询
            with self._cache_lock:
                cache[key] = payload
                cache.move_to_end(key)
                while len(cache) > 3:                        # 固定上限：最多驻留 3 个库
                    cache.popitem(last=False)               # 淘汰最久未用（LRU）
            return payload

    def _load_column_store(self, db_id: str) -> Dict[str, Any]:
        return self._cached_load(
            self._column_store_cache, db_id,
            lambda: self._read_column_store_payload(db_id),
            cache_name="column",
        )

    def _read_column_store_payload(self, db_id: str) -> Dict[str, Any]:
        vectors_path = self._workspace_root / "column_vector_store" / db_id / "vectors.npy"
        metadata_path = self._workspace_root / "column_vector_store" / db_id / "metadata.pkl"
        return _load_vector_store(vectors_path, metadata_path)

    def _load_value_desc_store(self, db_id: str) -> Dict[str, Any]:
        return self._cached_load(
            self._value_desc_store_cache, db_id,
            lambda: self._read_value_desc_store_payload(db_id),
            cache_name="value_desc",
        )

    def _read_value_desc_store_payload(self, db_id: str) -> Dict[str, Any]:
        vectors_path = self._workspace_root / "value_desc_vectors" / db_id / "vectors.npy"
        metadata_path = self._workspace_root / "value_desc_vectors" / db_id / "metadata.pkl"
        return _load_vector_store(vectors_path, metadata_path)

    def _load_lsh_index(self, db_id: str) -> Dict[str, Any]:
        return self._cached_load(
            self._lsh_cache, db_id,
            lambda: self._read_lsh_payload(db_id),
            cache_name="lsh",
        )

    def _read_lsh_payload(self, db_id: str) -> Dict[str, Any]:
        index_path = self._workspace_root / "lsh_indexes" / db_id / "lsh_index.pkl"
        if not index_path.exists():
            return {}
        with index_path.open("rb") as f:
            return pickle.load(f)


def _extract_db_name(db_table: str) -> str:
    raw = str(db_table or "")
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    parts = raw.split(".", 1)
    if len(parts) != 2:
        return ""
    return parts[0].strip()


def _parse_lsh_key(key: str) -> Dict[str, str]:
    raw = str(key or "")
    if ":" not in raw:
        return {"matched_column": "", "matched_value": ""}
    path, value = raw.rsplit(":", 1)
    return {"matched_column": path, "matched_value": value}


def _load_vector_store(vectors_path: Path, metadata_path: Path, values_key: str = "texts") -> Dict[str, Any]:
    if not vectors_path.exists() or not metadata_path.exists():
        return {"vectors": np.empty((0, 1), dtype=np.float32), "normalized": None, "metadata": [], values_key: []}
    vectors = np.load(str(vectors_path))
    with metadata_path.open("rb") as f:
        payload = pickle.load(f)
    metadata = payload.get("metadata") or []
    values = payload.get(values_key) or []
    if len(vectors.shape) == 1:
        vectors = vectors.reshape(1, -1)
    normalized = _normalize_rows(vectors) if vectors.size > 0 else None
    return {"vectors": vectors, "normalized": normalized, "metadata": metadata, values_key: values}


def _normalize_rows(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-8)


def _normalize_vector(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / max(norm, 1e-8)


def _text_match_score(keyword: str, text: str) -> float:
    key = _normalize_text(keyword)
    target = _normalize_text(text)
    if not key or not target:
        return 0.0
    if key in target or target in key:
        return 1.0
    key_tokens = {token for token in _TOKEN_SPLIT_PATTERN.split(key) if token}
    target_tokens = {token for token in _TOKEN_SPLIT_PATTERN.split(target) if token}
    if not key_tokens or not target_tokens:
        return 0.0
    overlap = len(key_tokens & target_tokens)
    union = len(key_tokens | target_tokens)
    return float(overlap) / float(union) if union > 0 else 0.0


def _normalize_text(value: str) -> str:
    return str(value or "").strip().lower().replace("`", "")


def _create_minhash(value: str, num_perm: int, k: int) -> MinHash:
    minhash = MinHash(num_perm=num_perm)
    text = str(value or "")
    max_iter = max(1, len(text) - k + 1)
    for index in range(max_iter):
        shingle = text[index:index + k]
        minhash.update(shingle.encode("utf-8"))
    return minhash
