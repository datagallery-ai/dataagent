"""Atomic JSON writes must tolerate concurrent job-result writers."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from dataagent.core.utils.json_store import read_json_object, write_json_object


def test_concurrent_atomic_writers_leave_complete_json(tmp_path, monkeypatch):
    destination = tmp_path / "result.json"
    payloads = [{"status": "cancelled", "summary": "取消" * 500}, {"status": "completed", "rows": list(range(500))}]
    ready = Barrier(2)
    replace = Path.replace

    def publish_together(source, target):
        if Path(target) == destination:
            # Both writes have finished before either rename; a shared .tmp
            # name reliably loses its second rename instead of failing by chance.
            ready.wait(timeout=5)
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", publish_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write_json_object, destination, payload) for payload in payloads]
        for future in futures:
            future.result(timeout=10)

    assert read_json_object(destination, {}) in payloads
    assert list(tmp_path.iterdir()) == [destination]


def test_failed_atomic_write_preserves_old_file_and_cleans_temp(tmp_path, monkeypatch):
    destination = tmp_path / "result.json"
    original = {"status": "running"}
    write_json_object(destination, original)

    def refuse_replace(source, target):
        raise OSError("replace refused")

    monkeypatch.setattr(Path, "replace", refuse_replace)
    with pytest.raises(OSError, match="replace refused"):
        write_json_object(destination, {"status": "completed"})

    assert read_json_object(destination, {}) == original
    assert list(tmp_path.iterdir()) == [destination]
