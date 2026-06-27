"""数据库执行工具 — 在只读连接上运行 SQLite 查询并归类结果状态

供 validator 与 evaluator 共用。单条查询的产物用 QueryRunOutcome 承载，
结果状态用 ExecStatus 枚举区分；缓存版与非缓存版共享同一段执行内核。
"""
import sqlite3
import logging
import threading
from enum import Enum
from functools import lru_cache
from dataclasses import dataclass
from typing import List, Any, Optional, Tuple

from tabulate import tabulate

logger = logging.getLogger(__name__)

# 文本预览的截断阈值：最多渲染前若干行，超长字符串单元格再行截断
_PREVIEW_MAX_ROWS = 5
_PREVIEW_MAX_CELL_CHARS = 100


class ExecStatus(str, Enum):
    """SQL 执行后的结果归类（继承 str 以便日志/比较直接当字符串使用）"""
    OK = "ok"               # 正常返回非空且含有效值的结果
    TIMED_OUT = "timed_out"  # 超出时间预算被中断
    NO_ROWS = "no_rows"     # 执行成功但零行
    ALL_NULL = "all_null"   # 有行但所有单元格均为 NULL
    ERROR = "error"         # 执行抛出异常


@dataclass
class QueryRunOutcome:
    """单条 SQL 的执行产物：状态 + 列/行数据 + 文本预览"""
    status: ExecStatus
    db_path: str
    sql: str
    columns: Optional[List[str]] = None
    rows: Optional[List[Tuple[Any, ...]]] = None
    preview: Optional[str] = None
    message: Optional[str] = None

    def __post_init__(self):
        # 有列与行数据时渲染表格预览，否则退回提示文案
        if self.columns is not None and self.rows is not None:
            self.preview = self._render_table_preview()
        else:
            self.preview = self.message

    def _render_table_preview(self) -> str:
        """将前若干行渲染为可读表格，超长字符串单元格做尾部省略"""
        def clip(cell: Any) -> Any:
            if isinstance(cell, str) and len(cell) > _PREVIEW_MAX_CELL_CHARS:
                return f"'{cell[:_PREVIEW_MAX_CELL_CHARS]}...'"
            return cell

        sampled = [[clip(cell) for cell in row]
                   for row in (self.rows or [])[:_PREVIEW_MAX_ROWS]]
        return tabulate(tabular_data=sampled, headers=self.columns, tablefmt="psql")


class _QueryWorker(threading.Thread):
    """在独立线程中执行查询，配合主线程的 join 超时实现可中断执行"""

    def __init__(self, db_path: str, sql: str, timeout: int = 30):
        super().__init__()
        self.db_path = db_path
        self.sql = sql
        self.timeout = timeout
        self.rows: Optional[List[Tuple[Any, ...]]] = None
        self.columns: Optional[List[str]] = None
        self.error: Optional[Exception] = None
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        def abort_if_cancelled():
            if self._cancel.is_set():
                raise TimeoutError(f"SQL execution timed out after {self.timeout} seconds")

        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                conn.text_factory = lambda x: str(x, "utf-8", errors="replace")
                conn.set_progress_handler(abort_if_cancelled, 1000)
                cursor = conn.cursor()
                cursor.execute(self.sql)
                self.columns = [d[0] for d in cursor.description]
                self.rows = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 - 统一收敛为执行错误
            self.error = exc


def _run_and_classify(db_path: str, sql: str, timeout: int) -> QueryRunOutcome:
    """执行查询并归类为 QueryRunOutcome（缓存/非缓存入口共用此内核）"""
    worker = _QueryWorker(db_path, sql, timeout)
    worker.daemon = True
    worker.start()
    worker.join(timeout)

    # 1) 线程仍在跑 → 超时，发出取消信号
    if worker.is_alive():
        worker.cancel()
        worker.join(1)
        return QueryRunOutcome(
            status=ExecStatus.TIMED_OUT, db_path=str(db_path), sql=sql,
            message=f"Query did not complete within the {timeout}s budget.",
        )

    # 2) 执行抛异常
    if worker.error is not None:
        return QueryRunOutcome(
            status=ExecStatus.ERROR, db_path=str(db_path), sql=sql,
            message=str(worker.error),
        )

    rows = worker.rows if worker.rows is not None else []

    # 3) 零行
    if len(rows) == 0:
        return QueryRunOutcome(
            status=ExecStatus.NO_ROWS, db_path=str(db_path), sql=sql,
            columns=worker.columns, rows=rows,
            message="Query executed successfully but returned no rows.",
        )

    # 4) 有行但全部为 NULL
    if not any(cell is not None for row in rows for cell in row):
        return QueryRunOutcome(
            status=ExecStatus.ALL_NULL, db_path=str(db_path), sql=sql,
            columns=worker.columns, rows=rows,
            message="Query returned rows whose values are entirely NULL.",
        )

    # 5) 正常结果
    return QueryRunOutcome(
        status=ExecStatus.OK, db_path=str(db_path), sql=sql,
        columns=worker.columns, rows=rows,
    )


@lru_cache(maxsize=1000)
def run_query(db_path: str, sql: str, timeout: int = 30) -> QueryRunOutcome:
    """执行 SQL 并返回归类结果（带 LRU 缓存）"""
    return _run_and_classify(db_path, sql, timeout)


def run_query_uncached(db_path: str, sql: str, timeout: int = 30) -> QueryRunOutcome:
    """执行 SQL 并返回归类结果（不走缓存，用于结果可能变化的场景）"""
    return _run_and_classify(db_path, sql, timeout)
