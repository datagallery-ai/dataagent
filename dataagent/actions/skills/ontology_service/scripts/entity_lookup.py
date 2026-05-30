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
"""Ontology entity lookup CLI for direct node and edge queries."""

from __future__ import annotations

import argparse

from cli_utils import add_connection_args, build_client_from_args, emit_output


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="Run direct ontology entity lookups.")
    add_connection_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    object_info = subparsers.add_parser("object-info", help="List nodes for one object type.")
    object_info.add_argument("--object-type", required=True)

    node_info = subparsers.add_parser("node-info", help="Get one node by UUID.")
    node_info.add_argument("--object-type", required=True)
    node_info.add_argument("--uuid", required=True)

    relation_info = subparsers.add_parser("relation-info", help="List edges for one relation type.")
    relation_info.add_argument("--relation-type", required=True)

    edge_info = subparsers.add_parser("edge-info", help="Get one edge by UUID.")
    edge_info.add_argument("--relation-type", required=True)
    edge_info.add_argument("--uuid", required=True)

    relation_by_start = subparsers.add_parser("relation-by-start", help="Find matching edges from start node UUID.")
    relation_by_start.add_argument("--start-node-uuid", required=True)
    relation_by_start.add_argument("--relation-type", required=True)

    relation_by_end = subparsers.add_parser("relation-by-end", help="Find matching edges from one end node UUID.")
    relation_by_end.add_argument("--end-node-uuid", required=True)
    relation_by_end.add_argument("--relation-type", required=True)

    return parser


def main() -> int:
    """解析参数并执行实体查询。"""
    parser = build_parser()
    args = parser.parse_args()
    client = build_client_from_args(args)

    if args.command == "object-info":
        result = client.get_object_info(args.object_type)
    elif args.command == "node-info":
        result = client.get_node_info(args.object_type, args.uuid)
    elif args.command == "relation-info":
        result = client.get_relation_info(args.relation_type)
    elif args.command == "edge-info":
        result = client.get_edge_info(args.relation_type, args.uuid)
    elif args.command == "relation-by-start":
        result = client.get_relation_by_start_uuid(args.start_node_uuid, args.relation_type)
    else:
        result = client.get_relation_by_end_uuid(args.end_node_uuid, args.relation_type)

    emit_output(result, compact=args.compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
