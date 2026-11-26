"""In-process question admission with fixed 15-minute error observation windows.

Halving never cancels an admitted question. All instances are owned by one runner;
there is no external controller, automatic increase, restart or whole-case retry.
Window changes apply only on process start. A separately authorized configuration
restart may interrupt in-flight work; ordinary window-based halving never does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path

SEMANTIC_TIMEOUT_TYPES = frozenset(
    {
        "TimeoutError",
        "Timeout",
        "ReadTimeout",
        "ConnectTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "ReadTimeoutError",
        "ConnectTimeoutError",
    }
)


class CaseAdmission:
    def __init__(
        self,
        directory,
        *,
        initial_limit=8,
        window_seconds=900,
        error_threshold=3,
        now=time.monotonic,
        sleep=asyncio.sleep,
    ):
        if type(initial_limit) is not int or initial_limit < 1:
            raise ValueError("initial_limit must be a positive integer")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        if type(error_threshold) is not int or error_threshold < 1:
            raise ValueError("error_threshold must be positive")
        self.directory = Path(directory)
        self.initial_limit = self.limit = initial_limit
        self.window_seconds = window_seconds
        self.error_threshold = error_threshold
        self.now, self.sleep = now, sleep
        self.active = 0
        self.started = False
        self.window_index = 0
        self.window_start = None
        self.requests = set()
        self.errors = {}
        self.semantic_timeouts = {}
        self.seen_requests = set()
        self.seen_errors = set()
        self.seen_semantic_timeouts = set()
        self.condition = asyncio.Condition()
        self.task = None
        self.last_window = None

    def _state(self):
        return {
            "pid": os.getpid(),
            "initial_limit": self.initial_limit,
            "target": self.limit,
            "active": self.active,
            "window_index": self.window_index,
            "window_start_monotonic": self.window_start,
            "window_end_monotonic": self.window_start + self.window_seconds,
            "monotonic_seconds": self.now(),
            "window_seconds": self.window_seconds,
            "error_threshold": self.error_threshold,
            "physical_attempts": len(self.requests),
            "llm_errors": len(self.errors),
            "llm_error_types": dict(Counter(self.errors.values())),
            "semantic_timeouts": len(self.semantic_timeouts),
            "semantic_timeout_types": dict(Counter(self.semantic_timeouts.values())),
            "combined_errors": len(self.errors) + len(self.semantic_timeouts),
            "last_window": self.last_window,
        }

    def _record(self, event, **values):
        state = {**self._state(), "event": event, **values}
        path = self.directory / "case-concurrency-state.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        temp.replace(path)
        with (self.directory / "case-concurrency-events.jsonl").open("a") as file:
            file.write(json.dumps(state, ensure_ascii=False) + "\n")

    async def __aenter__(self):
        if self.started:
            raise RuntimeError("CaseAdmission cannot be started twice")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.started = True
        self.window_start = self.now()
        self._record("start")
        self.task = asyncio.create_task(self._window_loop(), name="case-admission-windows")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.started:
            self._record("monitor_closed")

    def tick(self):
        if not self.started:
            raise RuntimeError("CaseAdmission is not started")
        current = self.now()
        while current >= self.window_start + self.window_seconds:
            old_limit = self.limit
            combined = len(self.errors) + len(self.semantic_timeouts)
            if combined >= self.error_threshold:
                self.limit = max(1, self.limit // 2)
            self.last_window = {
                "window_index": self.window_index,
                "start_monotonic": self.window_start,
                "end_monotonic": self.window_start + self.window_seconds,
                "physical_attempts": len(self.requests),
                "llm_errors": len(self.errors),
                "llm_error_types": dict(Counter(self.errors.values())),
                "semantic_timeouts": len(self.semantic_timeouts),
                "semantic_timeout_types": dict(Counter(self.semantic_timeouts.values())),
                "combined_errors": combined,
                "old_target": old_limit,
                "new_target": self.limit,
                "decision": "halve"
                if self.limit < old_limit
                else ("floor" if combined >= self.error_threshold else "hold"),
            }
            self._record("window_closed")
            self.window_start += self.window_seconds
            self.window_index += 1
            self.requests.clear()
            self.errors.clear()
            self.semantic_timeouts.clear()
            self._record("window_started")

    async def _window_loop(self):
        while True:
            await self.sleep(max(0, self.window_start + self.window_seconds - self.now()))
            self.tick()

    def on_physical_event(self, event):
        """Safe synchronous callback from this process's physical gate."""
        if not self.started or event.get("pid", os.getpid()) != os.getpid():
            return
        kind = event.get("event")
        attempt = str(event.get("attempt_id", ""))
        if not attempt or kind not in ("physical_request", "physical_failure"):
            return
        self.tick()
        if kind == "physical_request":
            if attempt in self.seen_requests:
                return
            self.seen_requests.add(attempt)
            self.requests.add(attempt)
        else:
            if attempt in self.seen_errors:
                return
            status = event.get("http_status")
            error_type = event.get("error_type")
            if status in (429, 504, 408):
                category = "HTTP" + str(status)
            elif event.get("is_timeout") or error_type in SEMANTIC_TIMEOUT_TYPES:
                category = str(error_type or "httpx.TimeoutException")
            else:
                return
            self.seen_errors.add(attempt)
            self.errors[attempt] = category
        self._record(kind)

    def record_semantic_timeout(self, case_id, error_type):
        if not self.started or error_type not in SEMANTIC_TIMEOUT_TYPES:
            return
        self.tick()
        if case_id in self.seen_semantic_timeouts:
            return
        self.seen_semantic_timeouts.add(case_id)
        self.semantic_timeouts[case_id] = error_type
        self._record("semantic_timeout", case_id=case_id, error_type=error_type)

    @contextlib.asynccontextmanager
    async def slot(self):
        async with self.condition:
            self.tick()
            while self.active >= self.limit:
                await self.condition.wait()
                self.tick()
            self.active += 1
            self._record("case_admitted")
        try:
            yield
        finally:
            async with self.condition:
                self.active -= 1
                self._record("case_released")
                self.condition.notify_all()

    async def run_items(self, items, run_case):
        """Bounded workers wait for a slot before claiming another question."""
        queue = asyncio.Queue()
        for item in items:
            queue.put_nowait(item)

        async def worker():
            while not queue.empty():
                async with self.slot():
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await run_case(item)
                    finally:
                        queue.task_done()

        async with asyncio.TaskGroup() as tasks:
            for _ in range(min(self.initial_limit, queue.qsize())):
                tasks.create_task(worker())
