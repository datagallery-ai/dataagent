from __future__ import annotations

import json
import sys
from typing import Any

from dataagent.core.suite.builtin_suites.data_analysis.tools.semantic.client import semantic_retrieve

MCP_PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "data-analysis-semantic"
SERVER_VERSION = "1.0.0"

SEMANTIC_RETRIEVE_TOOL = {
    "name": "semantic_retrieve",
    "description": (
        "Retrieve a structured semantic bundle for a natural-language business question. "
        "Returns tables, columns, join paths, metric definitions, knowledge evidence, "
        "and SQL examples suitable for downstream NL2SQL."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language business question to retrieve semantic context for.",
            }
        },
        "required": ["query"],
    },
}


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32000, "message": message},
    }


def _handle_request(payload: dict[str, Any]) -> dict[str, Any] | None:
    method = str(payload.get("method") or "").strip()
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    request_id = payload.get("id")

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "tools/list":
        return _response(request_id, {"tools": [SEMANTIC_RETRIEVE_TOOL]})

    if method == "tools/call":
        tool_name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if tool_name != "semantic_retrieve":
            return _error(request_id, f"unknown tool: {tool_name or '<empty>'}")
        try:
            bundle = semantic_retrieve(str(arguments.get("query") or ""))
            encoded = json.dumps(bundle, ensure_ascii=False)
            return _response(
                request_id,
                {
                    "content": [{"type": "text", "text": encoded}],
                    "structuredContent": bundle,
                    "isError": False,
                },
            )
        except Exception as exc:
            return _response(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )

    if request_id is None:
        return None
    return _error(request_id, f"unsupported method: {method or '<empty>'}")


def _write_message(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def run_stdio_server() -> None:
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        response = _handle_request(payload)
        if response is not None:
            _write_message(response)


def main() -> None:
    run_stdio_server()


if __name__ == "__main__":
    main()
