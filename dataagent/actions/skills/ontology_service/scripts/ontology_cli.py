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
from typing import Any

from cli_utils import RichHelpFormatter, add_connection_args, build_client_from_args, emit_output
from ontology_client import build_filter_dict, normalize_filter_dict


def _parse_kv(kv: str) -> tuple[str, Any]:
    """Parse KEY=VALUE string into a tuple. Value is converted to int/float if possible."""
    if '=' not in kv:
        raise ValueError(f"Invalid KEY=VALUE format: {kv}")
    key, value = kv.split('=', 1)
    key = key.strip()
    value = value.strip()
    try:
        if '.' in value:
            value = float(value)
        else:
            value = int(value)
    except ValueError:
        pass
    return key, value


def _parse_filter_args(args: Any) -> dict[str, Any]:
    """Parse deconstructed filter arguments from command line args."""
    equal: dict[str, Any] = {}
    not_equal: dict[str, Any] = {}
    contains: dict[str, Any] = {}
    starts_with: dict[str, Any] = {}
    ends_with: dict[str, Any] = {}
    gt: dict[str, Any] = {}
    gte: dict[str, Any] = {}
    lt: dict[str, Any] = {}
    lte: dict[str, Any] = {}
    is_null: list[str] = []
    is_not_null: list[str] = []
    in_list: dict[str, list[Any]] = {}

    def parse_dict_param(param_val: list | None, target: dict[str, Any]) -> None:
        if not param_val:
            return
        for item in param_val:
            try:
                k, v = _parse_kv(item)
                target[k] = v
            except ValueError:
                pass

    parse_dict_param(getattr(args, 'filter_equal', None), equal)
    parse_dict_param(getattr(args, 'filter_not_equal', None), not_equal)
    parse_dict_param(getattr(args, 'filter_contains', None), contains)
    parse_dict_param(getattr(args, 'filter_starts_with', None), starts_with)
    parse_dict_param(getattr(args, 'filter_ends_with', None), ends_with)
    parse_dict_param(getattr(args, 'filter_gt', None), gt)
    parse_dict_param(getattr(args, 'filter_gte', None), gte)
    parse_dict_param(getattr(args, 'filter_lt', None), lt)
    parse_dict_param(getattr(args, 'filter_lte', None), lte)

    if getattr(args, 'filter_is_null', None):
        is_null = args.filter_is_null
    if getattr(args, 'filter_is_not_null', None):
        is_not_null = args.filter_is_not_null

    if getattr(args, 'filter_in', None):
        for item in args.filter_in:
            if '=' not in item:
                continue
            k, v = item.split('=', 1)
            k = k.strip()
            values = [x.strip() for x in v.split(',')]
            parsed_values = []
            for val in values:
                try:
                    if '.' in val:
                        parsed_values.append(float(val))
                    else:
                        parsed_values.append(int(val))
                except ValueError:
                    parsed_values.append(val)
            in_list[k] = parsed_values

    return build_filter_dict(
        equal=equal if equal else None,
        not_equal=not_equal if not_equal else None,
        contains=contains if contains else None,
        starts_with=starts_with if starts_with else None,
        ends_with=ends_with if ends_with else None,
        gt=gt if gt else None,
        gte=gte if gte else None,
        lt=lt if lt else None,
        lte=lte if lte else None,
        is_null=is_null if is_null else None,
        is_not_null=is_not_null if is_not_null else None,
        in_list=in_list if in_list else None,
    )


def _build_filter_from_args(args: Any) -> dict[str, Any]:
    """Build filter_dict from deconstructed filter arguments or fall back to filter-dict."""
    has_deconstructed = any([
        getattr(args, 'filter_equal', None),
        getattr(args, 'filter_not_equal', None),
        getattr(args, 'filter_contains', None),
        getattr(args, 'filter_starts_with', None),
        getattr(args, 'filter_ends_with', None),
        getattr(args, 'filter_gt', None),
        getattr(args, 'filter_gte', None),
        getattr(args, 'filter_lt', None),
        getattr(args, 'filter_lte', None),
        getattr(args, 'filter_is_null', None),
        getattr(args, 'filter_is_not_null', None),
        getattr(args, 'filter_in', None),
    ])

    if has_deconstructed:
        return _parse_filter_args(args)

    if hasattr(args, 'filter_dict') and args.filter_dict:
        return normalize_filter_dict(args.filter_dict)

    return {}


ENTRYPOINT = "python scripts/ontology_cli.py"
SHARED_ARGUMENTS = {
    "--scene",
    "--ontology-url",
    "--search-base-url",
    "--action-base-url",
    "--timeout",
    "--compact",
}

COMMAND_CATALOG: list[dict[str, Any]] = [
    {
        "name": "catalog",
        "description": "Print the public command catalog for this skill.",
        "examples": [
            f"{ENTRYPOINT} catalog",
            f"{ENTRYPOINT} catalog --catalog-command property-filter",
        ],
    },
    {
        "name": "describe",
        "description": "Describe ontology schema, labels, relations, and attributes.",
        "examples": [f"{ENTRYPOINT} describe"],
    },
    {
        "name": "object-info",
        "description": "List all node instances of one object type.",
        "arguments": ["--object-type"],
        "examples": [f"{ENTRYPOINT} object-info --object-type Supplier"],
    },
    {
        "name": "node-info",
        "description": "Get one node's properties by object type and UUID.",
        "arguments": ["--object-type", "--uuid"],
        "examples": [f"{ENTRYPOINT} node-info --object-type MPart --uuid <uuid>"],
    },
    {
        "name": "relation-info",
        "description": "List all edge instances of one relation type.",
        "arguments": ["--relation-type"],
        "examples": [f"{ENTRYPOINT} relation-info --relation-type Fund-INVESTS-Company"],
    },
    {
        "name": "edge-info",
        "description": "Get one edge's properties by relation type and UUID.",
        "arguments": ["--relation-type", "--uuid"],
        "examples": [f"{ENTRYPOINT} edge-info --relation-type Fund-INVESTS-Company --uuid <uuid>"],
    },
    {
        "name": "relation-by-start",
        "description": "Find relation instances reachable from a start node UUID in one hop.",
        "arguments": ["--start-node-uuid", "--relation-type"],
        "examples": [f"{ENTRYPOINT} relation-by-start --start-node-uuid <uuid> --relation-type Fund-INVESTS-Company"],
    },
    {
        "name": "relation-by-end",
        "description": "Find relation instances reachable from an end node UUID in one hop.",
        "arguments": ["--end-node-uuid", "--relation-type"],
        "examples": [f"{ENTRYPOINT} relation-by-end --end-node-uuid <uuid> --relation-type Fund-INVESTS-Company"],
    },
    {
        "name": "property-filter",
        "description": "Filter nodes or edges with a property expression dict.",
        "arguments": ["--element-class", "--element-type", "--filter-dict", "[--get-all-properties]"],
        "examples": [
            f"{ENTRYPOINT} property-filter --element-class Fund --element-type NODE"
            f" --filter-dict '{{\"name\": \"CONTAINS \\'A\\'\"}}'"
        ],
    },
    {
        "name": "property-info",
        "description": "Get full property descriptions and values for one node or edge.",
        "arguments": ["--element-class", "--element-type", "--element-uuid"],
        "examples": [f"{ENTRYPOINT} property-info --element-class Fund --element-type NODE --element-uuid <uuid>"],
    },
    {
        "name": "count-search",
        "description": "Count nodes or edges matching a property filter.",
        "arguments": ["--element-class", "--element-type", "--filter-dict"],
        "examples": [f"{ENTRYPOINT} count-search --element-class Fund --element-type NODE --filter-dict '{{}}'"],
    },
    {
        "name": "aggregate-search",
        "description": "Run SUM, AVG, MIN, MAX, or COUNT over a target property.",
        "arguments": ["--element-class", "--element-type", "--target-property", "--agg", "--filter-dict"],
        "examples": [
            f"{ENTRYPOINT} aggregate-search --element-class Fund --element-type NODE"
            f" --target-property AAA --agg AVG --filter-dict '{{}}'"
        ],
    },
    {
        "name": "sorted-search",
        "description": "Return filtered rows ordered by one property.",
        "arguments": [
            "--element-class",
            "--element-type",
            "--filter-dict",
            "[--return-properties]",
            "[--sort-by]",
            "[--descending]",
        ],
        "examples": [
            f"{ENTRYPOINT} sorted-search --element-class Company --element-type NODE --filter-dict '{{}}'"
            f" --sort-by registered_capital --descending"
        ],
    },
    {
        "name": "hop",
        "description": "Run a multi-hop graph search from one UUID.",
        "arguments": ["--uuid", "--hop-num", "[--accurate]", "[--limit]", "[--offset]"],
        "examples": [f"{ENTRYPOINT} hop --uuid <uuid> --hop-num 2"],
    },
    {
        "name": "sub-graph",
        "description": "Get a two-hop neighbourhood subgraph centered on one node.",
        "arguments": ["--uuid", "[--limit]"],
        "examples": [f"{ENTRYPOINT} sub-graph --uuid <uuid> --limit 1000"],
    },
    {
        "name": "pattern",
        "description": "Run a start-relation-end pattern search.",
        "arguments": ["--start-object-type", "--relation-type", "--direction",
                      "--end-object-type", "--limit", "--offset"],
        "examples": [
            f"{ENTRYPOINT} pattern --start-object-type Fund --relation-type Fund-INVESTS-Company"
            f" --direction 'out' --end-object-type Company"
        ],
    },
    {
        "name": "list-actions",
        "description": "List ontology action definitions available in the current scene.",
        "examples": [f"{ENTRYPOINT} list-actions"],
    },
    {
        "name": "run-action",
        "description": "Run one server-declared ontology action by action name or action id.",
        "arguments": [
            "(--action-name | --action-id)",
            "[--instance-type]",
            "[--instance-api-name]",
            "[--instance-id]",
            "[--input-params]",
        ],
        "examples": [
            f"{ENTRYPOINT} run-action --action-name <action_name> --instance-api-name <entity_type>"
            f" --instance-id <uuid> --input-params '{{}}'"
        ],
    },
]


def _examples_block(*examples: str) -> str:
    """生成示例代码块。"""
    if not examples:
        return ""
    lines = ["Examples:"]
    lines.extend(f"  {example}" for example in examples)
    return "\n".join(lines)


def _notes_block(*notes: str) -> str:
    """生成注意事项代码块。"""
    if not notes:
        return ""
    lines = ["Notes:"]
    lines.extend(f"  - {note}" for note in notes)
    return "\n".join(lines)


def _set_epilog(
    parser: argparse.ArgumentParser, *, examples: list[str] | None = None, notes: list[str] | None = None
) -> None:
    """为解析器设置帮助文本的示例和注意事项。"""
    sections = []
    if examples:
        sections.append(_examples_block(*examples))
    if notes:
        sections.append(_notes_block(*notes))
    parser.epilog = "\n\n".join(section for section in sections if section)


def _action_to_argument_spec(action: argparse.Action) -> dict[str, Any] | None:
    """将 argparse Action 转换为参数规范字典。"""
    option_strings = getattr(action, "option_strings", None) or []
    if not option_strings or "--help" in option_strings:
        return None

    primary_name = next((item for item in option_strings if item.startswith("--")), option_strings[0])
    spec: dict[str, Any] = {
        "name": primary_name,
        "aliases": option_strings,
        "required": bool(getattr(action, "required", False)),
        "description": action.help or "",
        "scope": "shared" if primary_name in SHARED_ARGUMENTS else "command",
    }
    if getattr(action, "choices", None):
        spec["choices"] = list(action.choices)
    default = getattr(action, "default", argparse.SUPPRESS)
    if default is not argparse.SUPPRESS and default not in (None, False, ""):
        spec["default"] = default
    if getattr(action, "metavar", None):
        spec["metavar"] = action.metavar
    return spec


def _get_subparsers(parser: argparse.ArgumentParser):
    subparsers = getattr(parser, "_subparsers", None)
    return subparsers


def _catalog_payload(parser: argparse.ArgumentParser, command: str | None = None) -> dict[str, Any]:
    metadata_by_name = {item["name"]: item for item in COMMAND_CATALOG}
    subparser_action = None
    for action in getattr(parser, "_actions", []):
        action_type = type(action).__name__
        if action_type == "_SubParsersAction":
            subparser_action = action
            break
    if subparser_action is None:
        subparser_action = _get_subparsers(parser)
    if subparser_action is None:
        return {"entrypoint": ENTRYPOINT, "notes": [], "commands": []}
    commands = []
    for name, subparser in subparser_action.choices.items():
        if command and name != command:
            continue
        metadata = metadata_by_name.get(name, {"name": name})
        arguments = []
        for action in getattr(subparser, "_actions", []):
            spec = _action_to_argument_spec(action)
            if spec is not None:
                arguments.append(spec)
        commands.append(
            {
                "name": name,
                "description": metadata.get("description", subparser.description or ""),
                "arguments": arguments,
                "examples": metadata.get("examples", []),
            }
        )
    return {
        "entrypoint": ENTRYPOINT,
        "notes": [
            "Prefer this public CLI instead of reading implementation files.",
            "All commands print JSON to stdout.",
            "Structured arguments accept JSON strings unless noted otherwise.",
            "For command-specific flags, run `<command> --help`.",
        ],
        "commands": commands,
    }


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    common = argparse.ArgumentParser(add_help=False, formatter_class=RichHelpFormatter)
    add_connection_args(common)
    parser = argparse.ArgumentParser(
        description="Unified public CLI for the ontology_service skill."
        " Prefer this command over direct script inspection.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    _set_epilog(
        parser,
        examples=[
            f"{ENTRYPOINT} catalog",
            f"{ENTRYPOINT} describe",
            f"{ENTRYPOINT} property-filter --element-class Fund --element-type NODE --filter-dict '{{}}'",
        ],
        notes=[
            "Run `catalog` first if you are unsure which subcommand matches the task.",
            "Use `<command> --help` to see field-level filling rules and examples.",
        ],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser(
        "catalog",
        help="Print the public command catalog.",
        description="Print the public command catalog for this skill.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    catalog.add_argument(
        "--catalog-command",
        help="Optional command name to focus on, for example `property-filter`, `pattern`, or `list-actions`.",
    )
    _set_epilog(
        catalog,
        examples=[
            f"{ENTRYPOINT} catalog",
            f"{ENTRYPOINT} catalog --catalog-command property-filter",
        ],
    )

    describe = subparsers.add_parser(
        "describe",
        help="Describe ontology schema.",
        description="Fetch ontology object types, relation types, and node/edge attribute definitions.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    _set_epilog(
        describe,
        examples=[f"{ENTRYPOINT} describe"],
        notes=[
            "Run this first when object labels, relation labels, or attribute names are still unknown.",
        ],
    )

    object_info = subparsers.add_parser(
        "object-info",
        help="List nodes for one object type.",
        description="List all node instances of one ontology object type.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    object_info.add_argument(
        "--object-type",
        required=True,
        help="Ontology node label to query, for example `Supplier`, `MPart`, `Company`, or `Fund`.",
    )
    object_info.add_argument(
        "--limit",
        type=int,
        required=False,
        help="Maximum number of results to return.",
    )
    object_info.add_argument(
        "--offset",
        type=int,
        required=False,
        help="Number of results to skip.",
    )
    _set_epilog(object_info, examples=[f"{ENTRYPOINT} object-info --object-type Supplier"])

    node_info = subparsers.add_parser(
        "node-info",
        help="Get one node by UUID.",
        description="Get full information for one node instance identified by object type and UUID.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    node_info.add_argument(
        "--object-type",
        required=True,
        help="Ontology node label for the target node, for example `MPart` or `Supplier`.",
    )
    node_info.add_argument(
        "--uuid",
        required=True,
        help="Backend UUID of the node. This is not the business `id` field.",
    )
    _set_epilog(node_info, examples=[f"{ENTRYPOINT} node-info --object-type MPart --uuid <uuid>"])

    relation_info = subparsers.add_parser(
        "relation-info",
        help="List edges for one relation type.",
        description="List all edge instances of one ontology relation type.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    relation_info.add_argument(
        "--relation-type",
        required=True,
        help="Ontology edge label to query, for example `Fund-INVESTS-Company`.",
    )
    relation_info.add_argument(
        "--limit",
        type=int,
        required=False,
        help="Maximum number of results to return.",
    )
    relation_info.add_argument(
        "--offset",
        type=int,
        required=False,
        help="Number of results to skip.",
    )
    _set_epilog(relation_info, examples=[f"{ENTRYPOINT} relation-info --relation-type Fund-INVESTS-Company"])

    edge_info = subparsers.add_parser(
        "edge-info",
        help="Get one edge by UUID.",
        description="Get full information for one edge instance identified by relation type and UUID.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    edge_info.add_argument(
        "--relation-type",
        required=True,
        help="Ontology edge label for the target edge, for example `Fund-INVESTS-Company`.",
    )
    edge_info.add_argument(
        "--uuid",
        required=True,
        help="Backend UUID of the edge. This is not the source or target business id.",
    )
    _set_epilog(edge_info, examples=[f"{ENTRYPOINT} edge-info --relation-type Fund-INVESTS-Company --uuid <uuid>"])

    relation_by_start = subparsers.add_parser(
        "relation-by-start",
        help="Find matching edges from one start node UUID.",
        description="Find one-hop relation instances of one relation type starting from a node UUID.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    relation_by_start.add_argument(
        "--start-node-uuid",
        required=True,
        help="Start node UUID used as the graph traversal source.",
    )
    relation_by_start.add_argument(
        "--relation-type",
        required=True,
        help="Relation type to keep from the one-hop result set, for example `Fund-INVESTS-Company`.",
    )
    _set_epilog(
        relation_by_start,
        examples=[f"{ENTRYPOINT} relation-by-start --start-node-uuid <uuid> --relation-type Fund-INVESTS-Company"],
    )

    relation_by_end = subparsers.add_parser(
        "relation-by-end",
        help="Find matching edges from one end node UUID.",
        description="Find one-hop relation instances of one relation type ending at a node UUID.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    relation_by_end.add_argument(
        "--end-node-uuid",
        required=True,
        help="End node UUID used as the graph traversal anchor.",
    )
    relation_by_end.add_argument(
        "--relation-type",
        required=True,
        help="Relation type to keep from the one-hop result set, for example `Fund-INVESTS-Company`.",
    )
    _set_epilog(
        relation_by_end,
        examples=[f"{ENTRYPOINT} relation-by-end --end-node-uuid <uuid> --relation-type Fund-INVESTS-Company"],
    )

    property_filter = subparsers.add_parser(
        "property-filter",
        help="Filter nodes or edges by properties.",
        description="Filter nodes or edges with a property condition dictionary.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    property_filter.add_argument(
        "--element-class",
        required=True,
        help="Node label when `--element-type NODE`, or relation label when `--element-type EDGE`.",
    )
    property_filter.add_argument(
        "--element-type",
        required=True,
        choices=["NODE", "EDGE"],
        help="Whether `--element-class` names a node label or an edge label.",
    )
    property_filter.add_argument(
        "--filter-dict",
        default="{}",
        help=(
            "JSON or Python dict string mapping property name to a Cypher-style predicate fragment. "
            "Examples: '{}' , '{\"name\": \"CONTAINS \\'A\\'\"}' , "
            '\'{"amount": ">= 10000", "status": "= \\\'active\\\'"}\'.'
        ),
    )
    property_filter.add_argument(
        "--get-all-properties",
        action="store_true",
        help="Request full property payloads in each result row if the backend supports this flag.",
    )
    property_filter.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of results to return (prevents timeouts on large tables).",
    )
    property_filter.add_argument(
        "--offset",
        type=int,
        default=None,
        help="Number of results to skip (for pagination).",
    )
    _set_epilog(
        property_filter,
        examples=[
            f"{ENTRYPOINT} property-filter --element-class Fund --element-type NODE --filter-dict '{{}}'",
            f"{ENTRYPOINT} property-filter --element-class Fund --element-type NODE"
            f" --filter-dict '{{\"name\": \"CONTAINS \\'A\\'\"}}'",
        ],
        notes=[
            "Use object or relation labels returned by `describe`.",
            "Predicate values are backend expression strings, not raw scalar values.",
        ],
    )

    property_info = subparsers.add_parser(
        "property-info",
        help="Get full property info for one node or edge.",
        description="Return property names, descriptions, and values for one node or edge UUID.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    property_info.add_argument(
        "--element-class",
        required=True,
        help="Node label when `--element-type NODE`, or relation label when `--element-type EDGE`.",
    )
    property_info.add_argument(
        "--element-type",
        required=True,
        choices=["NODE", "EDGE"],
        help="Whether the target UUID belongs to a node or an edge.",
    )
    property_info.add_argument(
        "--element-uuid",
        required=True,
        help="UUID of the target node or edge.",
    )
    _set_epilog(
        property_info,
        examples=[f"{ENTRYPOINT} property-info --element-class Fund --element-type NODE --element-uuid <uuid>"],
    )

    count = subparsers.add_parser(
        "count-search",
        help="Count nodes or edges matching property filters.",
        description="Count rows matching a property condition dictionary.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    count.add_argument(
        "--element-class",
        required=True,
        help="Node label when `--element-type NODE`, or relation label when `--element-type EDGE`.",
    )
    count.add_argument(
        "--element-type",
        required=True,
        choices=["NODE", "EDGE"],
        help="Whether to count nodes or edges.",
    )
    count.add_argument(
        "--filter-dict",
        default="{}",
        help="JSON or Python dict string using the same predicate format as `property-filter`.",
    )
    _set_epilog(
        count,
        examples=[f"{ENTRYPOINT} count-search --element-class Fund --element-type NODE --filter-dict '{{}}'"],
    )

    aggregate = subparsers.add_parser(
        "aggregate-search",
        help="Run an aggregate query.",
        description="Run SUM, AVG, MIN, MAX, or COUNT over one target property.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    aggregate.add_argument(
        "--element-class",
        required=True,
        help="Node label when `--element-type NODE`, or relation label when `--element-type EDGE`.",
    )
    aggregate.add_argument(
        "--element-type",
        required=True,
        choices=["NODE", "EDGE"],
        help="Whether the aggregate targets nodes or edges.",
    )
    aggregate.add_argument(
        "--target-property",
        required=True,
        help="Numeric or aggregatable property name to aggregate, for example `AAA` or `registered_capital`.",
    )
    aggregate.add_argument(
        "--agg",
        required=True,
        choices=["SUM", "AVG", "MIN", "MAX", "COUNT"],
        help="Aggregate operator to apply.",
    )
    aggregate.add_argument(
        "--filter-dict",
        default="{}",
        help="Optional JSON or Python dict string restricting which rows enter the aggregate.",
    )
    _set_epilog(
        aggregate,
        examples=[
            f"{ENTRYPOINT} aggregate-search --element-class Fund --element-type NODE"
            f" --target-property AAA --agg AVG --filter-dict '{{}}'",
        ],
    )

    sorted_search = subparsers.add_parser(
        "sorted-search",
        help="Run a sorted ontology query.",
        description="Return filtered rows ordered by one property.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    sorted_search.add_argument(
        "--element-class",
        required=True,
        help="Node label when `--element-type NODE`, or relation label when `--element-type EDGE`.",
    )
    sorted_search.add_argument(
        "--element-type",
        required=True,
        choices=["NODE", "EDGE"],
        help="Whether the query targets nodes or edges.",
    )
    sorted_search.add_argument(
        "--filter-dict",
        default="{}",
        help="Optional JSON or Python dict string restricting which rows are returned.",
    )
    sorted_search.add_argument(
        "--return-properties",
        default="",
        help=(
            "Optional JSON or Python list string naming which properties to return, "
            'for example \'["name", "registered_capital"]\'. Leave empty for full rows.'
        ),
    )
    sorted_search.add_argument(
        "--sort-by",
        required=True,
        help="Property name used for ordering.",
    )
    sorted_search.add_argument(
        "--descending",
        action="store_true",
        help="Sort descending instead of ascending.",
    )
    sorted_search.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of results to return (prevents timeouts on large tables).",
    )
    sorted_search.add_argument(
        "--offset",
        type=int,
        default=None,
        help="Number of results to skip (for pagination).",
    )
    _set_epilog(
        sorted_search,
        examples=[
            f"{ENTRYPOINT} sorted-search --element-class Company --element-type NODE"
            f" --filter-dict '{{}}' --sort-by registered_capital",
            f"{ENTRYPOINT} sorted-search --element-class Company --element-type NODE"
            f' --filter-dict \'{{"industry": "= \\\'AI\\\'"}}\' --return-properties \'["name", "registered_capital"]\''
            f" --sort-by registered_capital --descending",
        ],
    )

    hop = subparsers.add_parser(
        "hop",
        help="Run a multi-hop search.",
        description="Traverse outward from one node UUID by hop distance.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    hop.add_argument(
        "--uuid",
        required=True,
        help="Start node UUID used as the traversal source.",
    )
    hop.add_argument(
        "--hop-num",
        required=True,
        type=int,
        help="Maximum hop count. Use a positive integer such as 1, 2, or 3.",
    )
    hop.add_argument(
        "--accurate",
        action="store_true",
        help="Require exactly `hop-num` hops. If omitted, return paths up to `hop-num` hops.",
    )
    hop.add_argument(
        "--limit",
        type=int,
        required=False,
        help="Maximum number of results to return.",
    )
    hop.add_argument(
        "--offset",
        type=int,
        required=False,
        help="Number of results to skip.",
    )
    _set_epilog(
        hop,
        examples=[
            f"{ENTRYPOINT} hop --uuid <uuid> --hop-num 2",
            f"{ENTRYPOINT} hop --uuid <uuid> --hop-num 2 --accurate",
        ],
    )

    sub_graph = subparsers.add_parser(
        "sub-graph",
        help="Get a node-centred subgraph.",
        description="Get a two-hop neighbourhood subgraph centered on one node UUID.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    sub_graph.add_argument(
        "--uuid",
        required=True,
        help="Center node UUID.",
    )
    sub_graph.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of nodes or edges to return, if the backend supports this parameter.",
    )
    _set_epilog(sub_graph, examples=[f"{ENTRYPOINT} sub-graph --uuid <uuid> --limit 1000"])

    pattern = subparsers.add_parser(
        "pattern",
        help="Run a simple start-relation-end pattern search.",
        description="Search for a one-relation path pattern between two object types.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    pattern.add_argument(
        "--start-object-type",
        required=True,
        help="Start node label, for example `Fund`.",
    )
    pattern.add_argument(
        "--relation-type",
        required=True,
        help="Relation label joining the two object types, for example `Fund-INVESTS-Company`.",
    )
    pattern.add_argument(
        "--direction",
        required=True,
        choices=["-", "out", "in"],
        help="Edge direction in the pattern: `-` undirected, `out` start to end, `in` end to start.",
    )
    pattern.add_argument(
        "--end-object-type",
        required=True,
        help="End node label, for example `Company`.",
    )
    pattern.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of results to return.",
    )
    pattern.add_argument(
        "--offset",
        type=int,
        default=None,
        help="Number of results to skip.",
    )
    _set_epilog(
        pattern,
        examples=[
            f"{ENTRYPOINT} pattern --start-object-type Fund --relation-type Fund-INVESTS-Company"
            f" --direction 'out' --end-object-type Company"
        ],
    )

    list_actions = subparsers.add_parser(
        "list-actions",
        help="List all available ontology actions.",
        description="List server-declared ontology action definitions for the current scene.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    _set_epilog(
        list_actions,
        examples=[f"{ENTRYPOINT} list-actions"],
        notes=["Use this before `run-action` when action names, ids, or parameters are unknown."],
    )

    run_action = subparsers.add_parser(
        "run-action",
        help="Run one ontology action.",
        description="Run one server-declared ontology action by action name or explicit action id.",
        parents=[common],
        formatter_class=RichHelpFormatter,
    )
    action_group = run_action.add_mutually_exclusive_group(required=True)
    action_group.add_argument(
        "--action-name",
        help="Human-readable action name as returned by `list-actions`.",
    )
    action_group.add_argument(
        "--action-id",
        help="Explicit backend action id. Use this if the action name is ambiguous or already known.",
    )
    run_action.add_argument(
        "--instance-type",
        default="entity",
        help="Backend instance type. Keep the default `entity` unless the action definition expects another value.",
    )
    run_action.add_argument(
        "--instance-api-name",
        default=None,
        help="Backend entity or API type expected by the action definition.",
    )
    run_action.add_argument(
        "--instance-id",
        default=None,
        help="Target entity id or UUID expected by the action definition.",
    )
    run_action.add_argument(
        "--input-params",
        default="{}",
        help="JSON or Python dict string containing action input parameters.",
    )
    _set_epilog(
        run_action,
        examples=[
            f"{ENTRYPOINT} run-action --action-name <action_name> --instance-api-name <entity_type>"
            f" --instance-id <uuid> --input-params '{{}}'",
            f"{ENTRYPOINT} run-action --action-id <action_id> --instance-api-name <entity_type>"
            f" --instance-id <uuid> --input-params '{{\"key\": \"value\"}}'",
        ],
        notes=["Call `list-actions` first to inspect available actions and their expected parameters."],
    )

    return parser


def main() -> int:
    """解析参数并执行对应的命令。"""
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "catalog":
        emit_output(_catalog_payload(parser, getattr(args, "catalog_command", None)), compact=args.compact)
        return 0

    client = build_client_from_args(args)

    if args.command == "describe":
        result = client.describe_ontology()
    elif args.command == "object-info":
        result = client.get_object_info(args.object_type, limit=args.limit, offset=args.offset)
    elif args.command == "node-info":
        result = client.get_node_info(args.object_type, args.uuid)
    elif args.command == "relation-info":
        result = client.get_relation_info(args.relation_type, limit=args.limit, offset=args.offset)
    elif args.command == "edge-info":
        result = client.get_edge_info(args.relation_type, args.uuid)
    elif args.command == "relation-by-start":
        result = client.get_relation_by_start_uuid(args.start_node_uuid, args.relation_type)
    elif args.command == "relation-by-end":
        result = client.get_relation_by_end_uuid(args.end_node_uuid, args.relation_type)
    elif args.command == "property-filter":
        result = client.property_filter(
            args.element_class,
            args.element_type,
            _build_filter_from_args(args),
            get_all_properties=args.get_all_properties or None,
            limit=args.limit,
            offset=args.offset,
        )
    elif args.command == "property-info":
        result = client.property_info_search(args.element_class, args.element_type, args.element_uuid)
    elif args.command == "count-search":
        result = client.count_search(args.element_class, args.element_type, _build_filter_from_args(args))
    elif args.command == "aggregate-search":
        result = client.aggregate_search(
            args.element_class,
            args.element_type,
            args.target_property,
            args.agg,
            _build_filter_from_args(args),
        )
    elif args.command == "sorted-search":
        result = client.sorted_search(
            args.element_class,
            args.element_type,
            _build_filter_from_args(args),
            args.return_properties or None,
            args.sort_by or None,
            ascending=not args.descending,
            limit=args.limit,
            offset=args.offset,
        )
    elif args.command == "hop":
        result = client.hop_search(args.uuid, args.hop_num, args.accurate, limit=args.limit, offset=args.offset)
    elif args.command == "sub-graph":
        result = client.get_sub_graph(args.uuid, limit=args.limit)
    elif args.command == "pattern":
        direction_map = {"-": "-", "out": "->", "in": "<-"}
        direction = direction_map.get(args.direction, args.direction)
        result = client.pattern_search(
            start_object_type=args.start_object_type,
            relation_type=args.relation_type,
            direction=direction,
            end_object_type=args.end_object_type,
            limit=args.limit,
            offset=args.offset,
        )
    elif args.command == "list-actions":
        result = client.list_actions()
    elif args.command == "run-action":
        result = client.run_action(
            action_name=args.action_name,
            action_id=args.action_id,
            instance_type=args.instance_type,
            instance_api_name=args.instance_api_name,
            instance_id=args.instance_id,
            input_params=args.input_params,
        )
    else:
        result = {"error": f"Unknown command: {args.command}"}

    emit_output(result, compact=args.compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
