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

"""Semantic-service and ontology fixtures for the bio_lab performance e2e test."""

import atexit
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from loguru import logger

BIO_LAB_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BIO_LAB_DIR / "config"

os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")
os.environ.setdefault("DATAAGENT_CACHE_ANCHOR", "1")
os.environ.setdefault("DATAAGENT_CACHE_BREAKPOINT_ANNOTATION", "1")


def _semantic_layer_url_for_proxy() -> str:
    configured_url = globals().get("_SEMANTIC_LAYER_URL")
    if isinstance(configured_url, str) and configured_url.strip():
        return configured_url.strip()
    return os.environ.get("SEMANTIC_SERVICE_URL", "").strip()


def _disable_proxy_env() -> None:
    """Strip inherited proxy settings while preserving direct-host bypasses.

    This avoids httpx/litellm picking up Clash Verge SOCKS/HTTP proxy settings
    from the parent shell when the test is launched via `uv run`. Keep
    NO_PROXY/no_proxy so requests does not fall back to macOS system proxies
    for real semantic-service hosts.
    """
    for key in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "FTP_PROXY",
        "ftp_proxy",
        "SOCKS_PROXY",
        "socks_proxy",
        "SOCKS5_PROXY",
        "socks5_proxy",
    ):
        os.environ.pop(key, None)

    semantic_url = _semantic_layer_url_for_proxy()
    if semantic_url:
        parsed = urlparse(semantic_url if "://" in semantic_url else f"http://{semantic_url}")
        _add_no_proxy_hosts([parsed.hostname, "localhost", "127.0.0.1"])


def _add_no_proxy_hosts(hosts: list[str | None]) -> None:
    """Append direct-connect hosts to both NO_PROXY spellings."""
    additions = [host for host in hosts if host]
    if not additions:
        return

    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key, "")
        if raw.strip() == "*":
            continue
        existing = [item.strip() for item in raw.split(",") if item.strip()]
        seen = set(existing)
        for host in additions:
            if host not in seen:
                existing.append(host)
                seen.add(host)
        os.environ[key] = ",".join(existing)


_disable_proxy_env()

# ---------------------------------------------------------------------------
# Inline MetaVisor mock server (pre-cached offline responses)
# ---------------------------------------------------------------------------
_MOCK_PORT = 0  # 0 = auto-resolve (random or --mock_port); set before _start_mock_metavisor
# Semantic-service routing. Ontology (get_ontology_description) and NL2SQL
# perceptor both go through SemanticServiceClient reading SEMANTIC_LAYER.base_url.
# Defaults to the inline mock server (offline-reproducible). CLI callers can
# select the real semantic layer explicitly with --semantic_layer semantic_layer
# and --semantic_layer_url; SEMANTIC_SERVICE_URL remains a backward-compatible
# source for the URL.
_SEMANTIC_LAYER_URL = os.getenv("SEMANTIC_SERVICE_URL", "").strip()
_SEMANTIC_LAYER_MODE = "semantic_layer" if _SEMANTIC_LAYER_URL else "mock"
_REAL_SEMANTIC_SERVICE_TIMEOUT = 180
_DEFAULT_AGENT_CONFIG_FILE = "main_config.yaml"
_mock_server: HTTPServer | None = None


def configure_semantic_layer(mode: str | None = None, url: str | None = None) -> None:
    """Configure whether e2e runs use the inline mock or a semantic-layer URL."""
    global _SEMANTIC_LAYER_MODE, _SEMANTIC_LAYER_URL

    selected_mode = (mode or _SEMANTIC_LAYER_MODE or "mock").strip().lower()
    if selected_mode in {"semantic-layer", "semantic_url", "url", "real"}:
        selected_mode = "semantic_layer"
    if selected_mode not in {"mock", "semantic_layer"}:
        raise ValueError("--semantic_layer must be either 'mock' or 'semantic_layer'")

    selected_url = (url or os.environ.get("SEMANTIC_SERVICE_URL", "")).strip()
    if selected_mode == "semantic_layer" and not selected_url:
        raise ValueError("--semantic_layer semantic_layer requires --semantic_layer_url or SEMANTIC_SERVICE_URL")

    _SEMANTIC_LAYER_MODE = selected_mode
    _SEMANTIC_LAYER_URL = selected_url if selected_mode == "semantic_layer" else ""
    _disable_proxy_env()


def get_semantic_layer_mode() -> str:
    return _SEMANTIC_LAYER_MODE


def get_semantic_layer_url() -> str:
    return _SEMANTIC_LAYER_URL


def uses_mock_semantic_layer() -> bool:
    return _SEMANTIC_LAYER_MODE == "mock"


def _semantic_service_timeout() -> int:
    """Return semantic-service timeout for real-service e2e runs."""
    raw_timeout = os.getenv("SEMANTIC_SERVICE_TIMEOUT", "").strip()
    if not raw_timeout:
        return _REAL_SEMANTIC_SERVICE_TIMEOUT
    timeout = int(raw_timeout)
    if timeout <= 0:
        raise ValueError("SEMANTIC_SERVICE_TIMEOUT must be positive")
    return timeout


def _apply_semantic_layer_config(config: dict) -> None:
    semantic_layer = config.setdefault("SEMANTIC_LAYER", {})
    if _SEMANTIC_LAYER_MODE == "semantic_layer":
        if not _SEMANTIC_LAYER_URL:
            raise ValueError("--semantic_layer semantic_layer requires --semantic_layer_url or SEMANTIC_SERVICE_URL")
        semantic_layer["base_url"] = _SEMANTIC_LAYER_URL
        # 真实服务可能是自签 https，跳过证书校验。
        semantic_layer["verify_ssl"] = False
        semantic_layer["timeout"] = _semantic_service_timeout()
    else:
        if _uses_relevant_ontology_tool(config):
            raise ValueError(
                "main_config_retrieve.yaml uses semantic/retrieve, which is backed by the real semantic-service LLM "
                "path and is not mocked. Use --semantic_layer semantic_layer --semantic_layer_url for retrieve "
                "ontology tests."
            )
        semantic_layer["base_url"] = f"http://localhost:{_MOCK_PORT}"


def _uses_relevant_ontology_tool(config: dict) -> bool:
    tools = config.get("TOOLS", {})
    if not isinstance(tools, dict):
        return False
    local_functions = tools.get("local_functions", [])
    if not isinstance(local_functions, list):
        return False
    return any(
        isinstance(item, dict) and item.get("function") == "get_relevant_ontology_description_tool"
        for item in local_functions
    )


def _load_metavisor_cache() -> dict[str, Any]:
    """Load pre-captured MetaVisor responses from the merged config JSON."""
    cache_path = CONFIG_DIR / "metavisor_responses.json"
    with open(cache_path, encoding="utf-8") as fh:
        return json.load(fh)


_MV_CACHE: dict[str, Any] | None = None


def _get_metavisor_cache() -> dict[str, Any]:
    global _MV_CACHE
    if _MV_CACHE is None:
        _MV_CACHE = _load_metavisor_cache()
        logger.info(f"Loaded {_MV_CACHE.__len__()} MetaVisor response(s) from config")
    return _MV_CACHE


class _ReusableHTTPServer(HTTPServer):
    """HTTPServer with SO_REUSEADDR so the port can be rebound immediately
    after the process exits, avoiding 'Address already in use' (TIME_WAIT)."""

    allow_reuse_address = True


class _MockMVHandler(BaseHTTPRequestHandler):
    """Serves pre-cached MetaVisor responses."""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        cache = _get_metavisor_cache()

        if path == "/api/semantic/v1/advanced-search/table-list":
            self._json(cache.get("table-list") or {"error": "table-list not cached"})
            return
        if path == "/api/semantic/v1/advanced-search/table-columns-info":
            tname = qs.get("tableName", [""])[0]
            self._json(cache.get(f"columns:{tname}") or {"error": f"{tname} not cached"})
            return
        if path == "/api/semantic/v1/advanced-search/joinable-tables":
            self._json(cache.get("joinable-tables") or {"error": "joinable-tables not cached"})
            return
        if path == "/api/semantic/v1/search/dsl":
            self._json(self._mock_search_dsl(cache))
            return
        if path in (
            "/api/semantic/v1/advanced-search/semantic-search-columns",
            "/api/semantic/v1/advanced-search/vector-search-table-desc",
            "/api/semantic/v1/advanced-search/semantic-search-tables",
        ):
            key = path.split("/")[-1]
            self._json(cache.get(key, {}))
            return
        self._error(404, f"Unknown endpoint: {path}")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        self._error(404, f"Unknown endpoint: {path}")

    @staticmethod
    def _table_key(qualified_name: str) -> str:
        parts = str(qualified_name or "").split(".")
        return ".".join(parts[:2]) if len(parts) >= 3 else str(qualified_name or "")

    @staticmethod
    def _table_list_items(cache: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        items: list[tuple[str, dict[str, Any]]] = []
        for item in cache.get("table-list") or []:
            if not isinstance(item, dict):
                continue
            for name, meta in item.items():
                if name:
                    items.append((str(name), meta if isinstance(meta, dict) else {}))
        return items

    def _mock_search_dsl(self, cache: dict[str, Any]) -> dict[str, Any]:
        names = ["db_name_en", "table_name", "table_description", "qualified_name"]
        values: list[list[str]] = []
        for full_name, meta in self._table_list_items(cache):
            db_name, _, table = full_name.partition(".")
            description = meta.get("table_description_enhanced") or meta.get("table_description") or ""
            values.append([db_name, table, description, full_name])
        return {"entities": None, "relations": None, "attributes": {"name": names, "values": values}}

    def _json(self, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, msg: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def _start_mock_metavisor() -> None:
    """Start the inline MetaVisor mock HTTP server in a daemon thread."""
    global _mock_server
    _mock_server = _ReusableHTTPServer(("127.0.0.1", _MOCK_PORT), _MockMVHandler)
    t = threading.Thread(target=_mock_server.serve_forever, daemon=True)
    t.start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", _MOCK_PORT), timeout=0.5):
                logger.info(f"MetaVisor mock HTTP server listening on http://127.0.0.1:{_MOCK_PORT}")
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"Mock MetaVisor server failed to start on port {_MOCK_PORT}")


def _stop_mock_metavisor() -> None:
    """Stop the inline MetaVisor mock HTTP server and release the socket fd."""
    global _mock_server
    if _mock_server is not None:
        try:
            _mock_server.shutdown()
            _mock_server.server_close()
        except Exception as exc:
            logger.warning(f"Failed to stop mock MetaVisor server cleanly: {exc}")
        finally:
            _mock_server = None


# Register atexit so the mock server is cleaned up even on ungraceful exits
# (e.g., SIGTERM, unhandled exceptions). Prevents TIME_WAIT "Address already
# in use" on subsequent runs.

atexit.register(_stop_mock_metavisor)


# ---------------------------------------------------------------------------
# Ontology description mock
# ---------------------------------------------------------------------------
def _load_ontology_fixture() -> dict[str, str]:
    """Return ontology description rendered from the semantic-service mock cache."""
    from dataagent.actions.tools.semantic_tool.ontology_query import (
        _fetch_columns,
        _fetch_entities,
        _fetch_relations,
        _render_ontology_description,
    )

    class _FixtureSemanticClient:
        def __init__(self, cache: dict[str, Any]):
            self.cache = cache

        def get_table_list(self, database_name: str, *, limit: int) -> list:
            return list(self.cache.get("table-list") or [])[:limit]

        def get_table_columns_info(self, table_name: str, *, limit: int) -> dict:
            columns = self.cache.get(f"columns:{table_name}") or {}
            return dict(list(columns.items())[:limit])

        def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list:
            del table_names
            return list(self.cache.get("joinable-tables") or [])[:limit]

    client = _FixtureSemanticClient(_get_metavisor_cache())
    entities = _fetch_entities(client, "")
    database = ""
    if entities:
        database = str(entities[0].get("table_name") or "").partition(".")[0]
    columns_by_table = {
        e["table_name"]: _fetch_columns(client, e["table_name"]) for e in entities if e.get("table_name")
    }
    relations = _fetch_relations(client, [e["table_name"] for e in entities if e.get("table_name")])
    return _render_ontology_description(entities, columns_by_table, relations, database, [database])


@contextmanager
def mock_ontology_description():
    """Patch semantic ontology description retrieval to return fixture data."""
    result = _load_ontology_fixture()
    logger.info("semantic ontology get_ontology_description() → mocked (config/metavisor_responses.json)")

    with patch(
        "dataagent.actions.tools.semantic_tool.ontology_query.get_ontology_description", lambda *, _tool_context: result
    ):
        yield


def get_mock_port() -> int:
    return _MOCK_PORT


def set_mock_port(port: int) -> None:
    global _MOCK_PORT
    _MOCK_PORT = port


apply_semantic_layer_config = _apply_semantic_layer_config
disable_proxy_env = _disable_proxy_env
start_mock_metavisor = _start_mock_metavisor
stop_mock_metavisor = _stop_mock_metavisor
