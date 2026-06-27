"""跨平台原子写入工具（共享实现）

提供 atomic_write_text / atomic_write_json / atomic_write_pickle / atomic_write_npy 等
原子写入 API，写入流程为：写入 *.{uuid}.tmp → os.replace 到目标路径。

Windows 上 os.replace 可能被反病毒/索引器/OneDrive 同步等进程瞬时持锁，抛
PermissionError [WinError 5]。本模块统一通过 _replace_with_retry 进行短退避重试，
10 次累计约 4-5 秒，足以覆盖绝大多数瞬时锁场景；Linux 下原生 rename(2) 即原子
替换，无重试触发。

本模块为原子写入工具的唯一实现，请统一从 ``nl2sql.common.atomic_io`` 导入。
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_REPLACE_MAX_RETRIES = 10
_REPLACE_BASE_DELAY = 0.05  # 50ms


def _replace_with_retry(src: str, dst: str) -> None:
    """os.replace 带指数退避重试，针对 Windows 瞬时文件锁错误。"""
    last_err: Exception | None = None
    for attempt in range(_REPLACE_MAX_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_err = exc
            # 退避：50ms → 100 → 200 → 400 → 800 → 800 ... 封顶 800ms
            delay = _REPLACE_BASE_DELAY * (2 ** min(attempt, 4))
            if attempt < _REPLACE_MAX_RETRIES - 1:
                logger.warning(
                    "atomic_io: os.replace attempt %d/%d failed (%s), retrying in %.0fms: %s -> %s",
                    attempt + 1, _REPLACE_MAX_RETRIES, exc, delay * 1000, src, dst,
                )
                time.sleep(delay)
    # 重试耗尽仍失败 → 外抛原始 PermissionError，保留可诊断性
    assert last_err is not None
    raise last_err


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    ensure_parent_dir(path)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w", encoding=encoding) as f:
        f.write(content)
    _replace_with_retry(str(tmp_path), str(path))


def atomic_write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    ensure_parent_dir(path)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _replace_with_retry(str(tmp_path), str(path))


def atomic_write_pickle(path: Path, payload: Any) -> None:
    path = Path(path)
    ensure_parent_dir(path)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp_path.open("wb") as f:
        pickle.dump(payload, f)
    _replace_with_retry(str(tmp_path), str(path))


def atomic_write_npy(path: Path, array: np.ndarray) -> None:
    path = Path(path)
    ensure_parent_dir(path)
    tmp_path = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with tmp_path.open("wb") as f:
        np.save(f, array)
    _replace_with_retry(str(tmp_path), str(path))


def read_json(path: Path, default: Any) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_pickle(path: Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with path.open("rb") as f:
        return pickle.load(f)
