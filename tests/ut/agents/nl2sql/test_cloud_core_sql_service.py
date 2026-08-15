# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""CloudCore SQL 出站走 httpx + outbound_tls。"""

from __future__ import annotations

from typing import Any

import httpx

from dataagent.agents.nl2sql.utils import sql_service
from dataagent.agents.nl2sql.utils.sql_service import CloudCoreConfig, CloudCoreService


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_httpx(monkeypatch, payload: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    seen_services: list[str] = []
    captured: dict[str, Any] = {}
    sentinel = object()
    captured["verify_sentinel"] = sentinel

    def fake_verify(service: str = "llm"):
        seen_services.append(service)
        return sentinel

    def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResponse(payload)

    monkeypatch.setattr(sql_service, "httpx_verify", fake_verify)
    monkeypatch.setattr(sql_service.httpx, "post", fake_post)
    return seen_services, captured


def _assert_cloud_core_timeout(timeout: httpx.Timeout) -> None:
    assert timeout.connect == 10.0
    assert timeout.read == 1800.0


def test_execute_posts_via_httpx_with_cloud_core_verify(monkeypatch) -> None:
    seen, captured = _patch_httpx(monkeypatch, {"success": True, "data": [{"col": 1}]})
    service = CloudCoreService(CloudCoreConfig(path="https://sql.example.test/query"))

    columns, rows, error = service.execute("SELECT 1")

    assert error is None
    assert columns == ["col"]
    assert rows == [(1,)]
    assert captured["url"] == "https://sql.example.test/query"
    assert captured["verify"] is captured["verify_sentinel"]
    assert seen == ["cloud_core"]
    _assert_cloud_core_timeout(captured["timeout"])


def test_explain_with_url_posts_via_httpx_with_cloud_core_verify(monkeypatch) -> None:
    seen, captured = _patch_httpx(monkeypatch, {"error": None})
    service = CloudCoreService(
        CloudCoreConfig(path="", explain_url="https://sql.example.test/explain"),
    )

    assert service.explain("SELECT 1") is None
    assert captured["url"] == "https://sql.example.test/explain"
    assert captured["verify"] is captured["verify_sentinel"]
    assert captured["params"] == {"auto_repair": "true", "format_sql": "false"}
    assert seen == ["cloud_core"]
    _assert_cloud_core_timeout(captured["timeout"])


def test_explain_without_url_posts_via_httpx_with_cloud_core_verify(monkeypatch) -> None:
    seen, captured = _patch_httpx(monkeypatch, {"success": True})
    service = CloudCoreService(CloudCoreConfig(path="https://sql.example.test/query"))

    assert service.explain("SELECT 1") is None
    assert captured["url"] == "https://sql.example.test/query"
    assert captured["verify"] is captured["verify_sentinel"]
    assert seen == ["cloud_core"]
    _assert_cloud_core_timeout(captured["timeout"])
