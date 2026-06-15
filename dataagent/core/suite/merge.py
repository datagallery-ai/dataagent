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
"""Unified configuration layer merge (``merge_layers``)."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

WORKFLOW_TOP_KEYS: frozenset[str] = frozenset({"ACTOR_LOOP", "PRE_WORKFLOW", "POST_WORKFLOW"})


def extract_user_layer(interpolated: Mapping[str, Any], user_config: Mapping[str, Any]) -> dict[str, Any]:
    """
    Build the user merge layer from interpolated settings and original user YAML.

    Only paths present in ``user_config`` are included. Sibling keys merged from default
    into ``interpolated`` must not be promoted into the user layer.

    Args:
        interpolated: Post-interpolation full settings (``tmp``).
        user_config: Raw user YAML mapping (pre-``merge_layers``).

    Returns:
        User merge layer with interpolated leaf values at user-written paths only.
    """
    return _extract_user_subtree(interpolated, user_config)


def _extract_user_subtree(interpolated: Any, user_sub: Any) -> Any:
    """Recursively copy interpolated values along paths declared in ``user_sub``."""
    if isinstance(user_sub, Mapping):
        if not isinstance(interpolated, Mapping):
            return copy.deepcopy(user_sub)
        result: dict[str, Any] = {}
        for key, user_value in user_sub.items():
            if key in interpolated:
                result[key] = _extract_user_subtree(interpolated[key], user_value)
            else:
                result[key] = copy.deepcopy(user_value)
        return result
    if isinstance(user_sub, Sequence) and not isinstance(user_sub, (str, bytes)):
        if not user_sub:
            return []
        if all(isinstance(item, Mapping) for item in user_sub):
            if isinstance(interpolated, Sequence) and not isinstance(interpolated, (str, bytes)):
                return [
                    _extract_user_subtree(interpolated[idx] if idx < len(interpolated) else {}, item)
                    for idx, item in enumerate(user_sub)
                ]
            return copy.deepcopy(user_sub)
        if isinstance(interpolated, Sequence) and not isinstance(interpolated, (str, bytes)):
            return copy.deepcopy(interpolated)
        return copy.deepcopy(user_sub)
    if not isinstance(interpolated, Mapping):
        return copy.deepcopy(interpolated)
    return copy.deepcopy(user_sub)


def merge_layers(layers: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """
    Merge configuration layers from low to high priority into one dict.

    Layers must already be ordered default → suites (ascending priority) → user (highest).

    Args:
        layers: Ordered mapping layers; omitted keys do not participate.

    Returns:
        Merged configuration dict.
    """
    result: dict[str, Any] = {}
    for layer in layers:
        if not layer:
            continue
        _merge_mapping_into(result, layer, path=())
    return result


def _merge_mapping_into(target: dict[str, Any], source: Mapping[str, Any], *, path: tuple[str, ...]) -> None:
    """Recursively merge one layer mapping into ``target``."""
    for key, value in source.items():
        child_path = path + (key,)
        if key not in target:
            target[key] = copy.deepcopy(value)
            continue
        target[key] = _merge_values(target[key], value, path=child_path)


def _merge_values(existing: Any, incoming: Any, *, path: tuple[str, ...]) -> Any:
    """Merge two values at the same config path."""
    if isinstance(existing, Mapping) and isinstance(incoming, Mapping):
        merged = copy.deepcopy(existing)
        _merge_mapping_into(merged, incoming, path=path)
        return merged
    if isinstance(existing, list) and isinstance(incoming, list):
        if len(path) == 1 and path[0] in WORKFLOW_TOP_KEYS:
            return _merge_workflow_list(existing, incoming)
        return _merge_list_append(existing, incoming)
    return copy.deepcopy(incoming)


def _merge_list_append(existing: list[Any], incoming: list[Any]) -> list[Any]:
    """Append-merge lists with higher-priority ``incoming`` before ``existing``."""
    return copy.deepcopy(incoming) + copy.deepcopy(existing)


def _merge_workflow_list(existing: list[Any], incoming: list[Any]) -> list[Any]:
    """Structure-merge workflow node lists by ``node`` field."""
    order: list[str] = []
    by_node: dict[str, dict[str, Any]] = {}
    for item in existing:
        if not isinstance(item, Mapping):
            continue
        node = str(item.get("node") or "").strip()
        if not node:
            continue
        if node not in by_node:
            order.append(node)
        by_node[node] = copy.deepcopy(item)
    for item in incoming:
        if not isinstance(item, Mapping):
            continue
        node = str(item.get("node") or "").strip()
        if not node:
            continue
        if node not in by_node:
            by_node[node] = copy.deepcopy(item)
            order.append(node)
        else:
            by_node[node] = _merge_node(by_node[node], item)
    return [by_node[node] for node in order]


def _merge_node(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Merge one workflow node record; higher-priority ``incoming`` wins scalars and prepends lists."""
    result: dict[str, Any] = copy.deepcopy(existing)
    for key, value in incoming.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
            continue
        current = result[key]
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            nested = copy.deepcopy(current)
            _merge_mapping_into(nested, value, path=(key,))
            result[key] = nested
        elif isinstance(current, list) and isinstance(value, list):
            result[key] = _merge_list_append(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result
