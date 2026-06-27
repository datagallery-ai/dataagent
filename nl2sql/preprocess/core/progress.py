from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...common.atomic_io import atomic_write_json, read_json

# 北京时间（UTC+8）—— state 文件与日志统一使用此时区
_BEIJING_TZ = timezone(timedelta(hours=8))


def _now_beijing_iso() -> str:
    """返回北京时间 ISO 格式（含 +08:00 偏移），微秒精度。"""
    return datetime.now(tz=_BEIJING_TZ).isoformat(timespec="microseconds")


def _parse_iso(ts: str) -> Optional[datetime]:
    """解析任意 ISO 时间戳（含 Z / +08:00 / +00:00），失败返回 None。"""
    if not ts:
        return None
    try:
        # Python <3.11 不支持 "Z"，做兼容
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@dataclass
class ProgressStore:
    path: Path

    def load(self) -> Dict[str, Any]:
        return read_json(
            self.path,
            default={
                "status": "idle",
                "completed_keys": [],
                "updated_at": "",
                "history": [],
            },
        )

    def save(self, payload: Dict[str, Any]) -> None:
        payload = dict(payload)
        payload["updated_at"] = _now_beijing_iso()
        atomic_write_json(self.path, payload)

    # ── 执行历史追加：每次 run 在 history 里 append 一条，绝不覆盖既有条目 ──

    def mark_running(self) -> None:
        """标记开始执行：append 一条 history 条目，记录 execution_start_time。"""
        state = self.load()
        history: List[Dict[str, Any]] = list(state.get("history") or [])
        run_index = len(history) + 1
        history.append({
            "run_index": run_index,
            "status": "running",
            "execution_start_time": _now_beijing_iso(),
            "execution_end_time": None,
            "duration_seconds": None,
            "result": None,
            "error": None,
        })
        state["history"] = history
        state["status"] = "running"
        # 顶层 result/error 在 mark_running 阶段不主动清理，保留上一轮信息直至下一轮 mark_success/_failed 覆盖；
        # 这样若用户中途 ctrl-c，state 中既有历史完整 history，又有上轮顶层 result/error 可读。
        self.save(state)

    def _close_last_history(
        self,
        state: Dict[str, Any],
        final_status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """关闭 history 中最后一条 running 记录；若不存在则补登记一条（兼容历史 state）。"""
        history: List[Dict[str, Any]] = list(state.get("history") or [])
        end_ts = _now_beijing_iso()
        target: Optional[Dict[str, Any]] = None
        if history and history[-1].get("status") == "running":
            target = history[-1]
        else:
            # 兼容：旧 state 没有 history 或上一轮 mark_running 缺失
            target = {
                "run_index": len(history) + 1,
                "status": "running",
                "execution_start_time": end_ts,  # 无法回溯，记为同一时刻
                "execution_end_time": None,
                "duration_seconds": 0.0,
                "result": None,
                "error": None,
            }
            history.append(target)
        target["status"] = final_status
        target["execution_end_time"] = end_ts
        start_dt = _parse_iso(str(target.get("execution_start_time") or ""))
        end_dt = _parse_iso(end_ts)
        if start_dt and end_dt:
            target["duration_seconds"] = round((end_dt - start_dt).total_seconds(), 6)
        if result is not None:
            target["result"] = result
        if error is not None:
            target["error"] = error
        state["history"] = history

    def mark_success(self, **extra: Any) -> None:
        """标记成功：关闭 history 末条 + 更新顶层 status/result（保持兼容）。"""
        state = self.load()
        result_payload = extra.get("result") if isinstance(extra.get("result"), dict) else None
        self._close_last_history(state, final_status="success", result=result_payload, error=None)
        state["status"] = "success"
        # 清理上一轮失败遗留的顶层 error（顶层只反映最近一次执行）
        state.pop("error", None)
        state.update(extra)
        self.save(state)

    def mark_failed(self, error: str, **extra: Any) -> None:
        """标记失败：关闭 history 末条 + 更新顶层 status/error（保持兼容）。"""
        state = self.load()
        result_payload = extra.get("result") if isinstance(extra.get("result"), dict) else None
        self._close_last_history(state, final_status="failed", result=result_payload, error=error)
        state["status"] = "failed"
        state["error"] = error
        state.update(extra)
        self.save(state)

    # ── completed_keys 仍是累积更新，与历史不同语义 ──

    def completed_keys(self) -> List[str]:
        state = self.load()
        items = state.get("completed_keys") or []
        return [str(item) for item in items]

    def add_completed_key(self, key: str) -> None:
        if not key:
            return
        state = self.load()
        completed = {str(item) for item in (state.get("completed_keys") or [])}
        if key in completed:
            return
        completed.add(key)
        state["completed_keys"] = sorted(completed)
        self.save(state)
