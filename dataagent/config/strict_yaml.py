"""Bounded YAML 1.2 Core configuration reader with source-free diagnostics."""

import math
import re
from pathlib import Path

import yaml
from yaml.events import AliasEvent, DocumentStartEvent
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

MAX_DEPTH = 64
MAX_INTEGER = 2**53 - 1


class CoreLoader(yaml.SafeLoader):
    yaml_implicit_resolvers = {}


for kind, pattern, initials in (
    ("null", r"^(?:~|null|Null|NULL|)$", "~nN"),
    ("bool", r"^(?:true|True|TRUE|false|False|FALSE)$", "tTfF"),
    ("int", r"^(?:[+-]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$", "-+0123456789"),
    (
        "float",
        r"^(?:[+-]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+|\.(?:inf|Inf|INF))|\.(?:nan|NaN|NAN))$",
        "-+0123456789.",
    ),
):
    CoreLoader.add_implicit_resolver(
        "tag:yaml.org,2002:" + kind,
        re.compile(pattern),
        [*initials, ""] if kind == "null" else list(initials),
    )


def check_yaml_path(path: Path) -> None:
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError(f"{path}: YAML extension required")


def parse_yaml(text: str, path: Path | str = "<yaml>") -> dict:
    def fail(category, mark=None):
        position = f" at {mark.line + 1}:{mark.column + 1}" if mark else ""
        raise ValueError(f"{path}: {category}{position}") from None

    try:
        depth = 0
        for event in yaml.parse(text, Loader=CoreLoader):
            if isinstance(event, DocumentStartEvent) and event.version not in (None, (1, 2)):
                fail("YAML version must be 1.2", event.start_mark)
            if isinstance(event, AliasEvent) or getattr(event, "anchor", None):
                fail("anchors and aliases are forbidden", event.start_mark)
            if getattr(event, "tag", None):
                fail("explicit tags are forbidden", event.start_mark)
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    fail("maximum YAML depth exceeded", event.start_mark)
            if isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
        root = yaml.compose(text, Loader=CoreLoader)

        def convert(node):
            if isinstance(node, MappingNode):
                result = {}
                for key, value in node.value:
                    if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                        fail("mapping keys must be strings", key.start_mark)
                    if key.value == "<<":
                        fail("merge keys are forbidden", key.start_mark)
                    if key.value in result:
                        fail("duplicate mapping key", key.start_mark)
                    result[key.value] = convert(value)
                return result
            if isinstance(node, SequenceNode):
                return [convert(item) for item in node.value]
            kind = node.tag.rsplit(":", 1)[-1]
            if kind == "str":
                return node.value
            if kind == "null":
                return None
            if kind == "bool":
                return node.value.lower() == "true"
            if kind == "int":
                raw = node.value.lstrip("+-")
                if len(raw.lstrip("0")) > 32:
                    fail("number outside safe range; use a quoted string", node.start_mark)
                base = 8 if raw.startswith("0o") else 16 if raw.startswith("0x") else 10
                digits = raw[2:] if base != 10 else raw
                value = int(digits.lstrip("0") or "0", base)
                if node.value.startswith("-"):
                    value = -value
            else:
                value = float(
                    node.value.replace(".inf", "inf")
                    .replace(".Inf", "inf")
                    .replace(".INF", "inf")
                    .replace(".nan", "nan")
                    .replace(".NaN", "nan")
                    .replace(".NAN", "nan")
                )
            if not math.isfinite(value) or (float(value).is_integer() and abs(value) > MAX_INTEGER):
                fail("number outside safe range; use a quoted string", node.start_mark)
            return value

        if not isinstance(root, MappingNode):
            fail("expected a nonempty mapping document")
        return convert(root)
    except yaml.YAMLError as error:
        fail("invalid YAML syntax", getattr(error, "problem_mark", None))
    except (RecursionError, OverflowError):
        fail("YAML resource limit exceeded")


def read_yaml(path: Path) -> dict:
    path = Path(path)
    check_yaml_path(path)
    try:
        return parse_yaml(path.read_text(encoding="utf-8"), path)
    except UnicodeError:
        raise ValueError(f"{path}: invalid UTF-8") from None
