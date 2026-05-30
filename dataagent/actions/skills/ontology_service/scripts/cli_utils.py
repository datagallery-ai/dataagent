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
from __future__ import annotations

import argparse
import json
from typing import Any

from ontology_client import OntologyClient


class RichHelpFormatter(argparse.RawTextHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    """结合原始文本帮助和参数默认值的格式化器。"""

    pass


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    """为解析器添加通用的连接参数（URL、超时等）。"""
    parser.add_argument(
        "--scene",
        help="Optional `scene_name` query parameter. Use this when one deployment serves multiple ontology scenes.",
    )
    parser.add_argument(
        "--ontology-url",
        help=(
            "Base ontology URL. You can pass a plain URL or a JSON/Python dict string keyed by scene name, "
            'for example \'{"prod": "https://...", "default": "https://..."}\'.'
        ),
    )
    parser.add_argument(
        "--search-base-url",
        help="Explicit search endpoint base URL. If omitted, it is derived as <ontology-url>/api/v1/search.",
    )
    parser.add_argument(
        "--action-base-url",
        help=(
            "Explicit action endpoint base URL. If omitted, it is derived as "
            "<ontology-url>/api/v1/action/ontologies/actions/execute."
        ),
    )
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds for each request.")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print one-line JSON instead of pretty-printed JSON. Useful when another tool parses stdout.",
    )


def build_client_from_args(args: argparse.Namespace) -> OntologyClient:
    """从命令行参数构建 OntologyClient 实例。"""
    return OntologyClient.from_env(
        scene=getattr(args, "scene", None),
        ontology_url=getattr(args, "ontology_url", None),
        search_base_url=getattr(args, "search_base_url", None),
        action_base_url=getattr(args, "action_base_url", None),
        timeout=getattr(args, "timeout", 120),
    )


def emit_output(data: Any, *, compact: bool = False) -> None:
    """将数据输出为 JSON 格式到标准输出。"""
    indent = None if compact else 2
    separators = (",", ":") if compact else None
    print(json.dumps(data, ensure_ascii=False, indent=indent, separators=separators, default=str))
