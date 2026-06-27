"""Runner 共享工具

提供以下公共组件：
  - BeijingFormatter: 北京时间日志格式化器
  - ErrorManager: 线程安全的 errors.json 管理器
  - setup_logging(): 配置控制台 + 文件双输出日志
"""
import json
import logging
import sys
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List

from .atomic_io import atomic_write_json


class BeijingFormatter(logging.Formatter):
    """北京时间格式化器（UTC+8）"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=timezone(timedelta(hours=8)))
        return dt.strftime(datefmt) if datefmt else dt.strftime("%Y-%m-%d %H:%M:%S")


def setup_logging(log_dir: Path, run_id: str, prefix: str, verbose: bool = False) -> Path:
    """配置日志：控制台 + 文件双输出，返回日志文件路径

    Args:
        log_dir: 日志根目录（如 config.STEP3B_LOG_DIR 或 config.STEP3_LOG_DIR）
        run_id: 运行 ID，用于子目录
        prefix: 日志文件前缀（如 "step3b" / "step3"）
        verbose: 是否启用 DEBUG 级别

    Returns:
        日志文件完整路径
    """
    run_log_dir = log_dir / run_id
    run_log_dir.mkdir(parents=True, exist_ok=True)
    # 文件名时间戳统一使用北京时间（与日志内容时区一致）
    bj_tz = timezone(timedelta(hours=8))
    timestamp = datetime.now(tz=bj_tz).strftime("%Y%m%d_%H%M%S")
    log_file = run_log_dir / f"{prefix}_{timestamp}.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    formatter = BeijingFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setFormatter(formatter)

    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=log_level, handlers=[console_handler, file_handler])
    return log_file


class ErrorManager:
    """线程安全的 errors.json 管理器

    错误文件格式：{"721": "LLMMaxRetriesExceeded: ...", "42": "..."}
    """

    def __init__(self, error_file: Path):
        self._file = error_file
        self._lock = threading.Lock()
        self._errors: Dict[str, str] = self._load()

    def _load(self) -> Dict[str, str]:
        """加载已有 errors.json（若存在）"""
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def add(self, question_id: int, error_msg: str):
        """添加错误并立即持久化（线程安全）"""
        with self._lock:
            self._errors[str(question_id)] = error_msg
            self._save()

    def remove(self, question_id: int):
        """移除已修复的错误（线程安全）"""
        with self._lock:
            key = str(question_id)
            if key in self._errors:
                del self._errors[key]
                self._save()

    def get_failed_ids(self) -> List[int]:
        """获取所有失败的 question_id"""
        with self._lock:
            return [int(k) for k in self._errors.keys()]

    def count(self) -> int:
        """当前错误数"""
        with self._lock:
            return len(self._errors)

    def _save(self):
        """写入 errors.json（调用方已持有锁）—— 走原子写入，避免半截 JSON"""
        atomic_write_json(self._file, self._errors)
