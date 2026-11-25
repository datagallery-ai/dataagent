# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
# ============================================================================
"""流式静默窗 watchdog 的可配置项 UT（_stream_silent_window）。

watchdog 本体在 async _astream_iter 内（本 venv 无 pytest-asyncio，async 用例
为已知环境假失败基线），此处覆盖 sync 可测面：环境变量解析/缺省/非法值/下限钳制。
"""

from __future__ import annotations

from dataagent.core.managers.llm_manager.llm_client import _stream_silent_window


class TestStreamSilentWindow:
    def test_default_600_when_unset(self, monkeypatch):
        monkeypatch.delenv("DATAAGENT_STREAM_SILENT_WINDOW", raising=False)
        assert _stream_silent_window() == 600.0

    def test_env_override_parsed(self, monkeypatch):
        monkeypatch.setenv("DATAAGENT_STREAM_SILENT_WINDOW", "120")
        assert _stream_silent_window() == 120.0

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DATAAGENT_STREAM_SILENT_WINDOW", "not-a-number")
        assert _stream_silent_window() == 600.0

    def test_lower_clamp_30s(self, monkeypatch):
        monkeypatch.setenv("DATAAGENT_STREAM_SILENT_WINDOW", "5")
        assert _stream_silent_window() == 30.0

    def test_empty_string_uses_default(self, monkeypatch):
        monkeypatch.setenv("DATAAGENT_STREAM_SILENT_WINDOW", "")
        assert _stream_silent_window() == 600.0
