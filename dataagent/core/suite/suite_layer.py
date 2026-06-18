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
"""Build per-Suite layer contributions from bundle files."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from dataagent.core.suite.types import SuiteRecord


def build_suite_layers(
    suites: Sequence[SuiteRecord],
    *,
    default_actor_nodes: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """
    Build ``suite_layer`` dicts and ``activated_suites`` metadata for merge.

    Args:
        suites: Activated suites in low → high merge order.
        default_actor_nodes: ``node`` names allowed for Suite ACTOR_LOOP patches.

    Returns:
        Tuple of (suite_layers, activated_suites metadata).
    """
    layers: list[dict[str, Any]] = []
    activated_meta: list[dict[str, str]] = []
    for suite in suites:
        layer, unknown_nodes = _build_one_suite_layer(suite, default_actor_nodes=default_actor_nodes)
        if unknown_nodes:
            joined = ", ".join(sorted(unknown_nodes))
            raise ValueError(
                f"Suite {suite.name!r} references unknown ACTOR_LOOP nodes: {joined}. "
                "Suites may only patch existing default nodes."
            )
        if layer:
            layers.append(layer)
        activated_meta.append({"name": suite.name, "root": str(suite.root)})
    return layers, activated_meta


def _build_one_suite_layer(
    suite: SuiteRecord,
    *,
    default_actor_nodes: set[str],
) -> tuple[dict[str, Any], set[str]]:
    """Assemble one Suite layer and collect unknown ACTOR_LOOP node names."""
    root = suite.root
    layer: dict[str, Any] = {}
    unknown_nodes: set[str] = set()

    models_yaml = root / "models.yaml"
    if models_yaml.is_file():
        with open(models_yaml, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle) or {}
        if isinstance(doc, Mapping):
            layer.update(dict(doc))

    tools_layer = _load_tools_layer(root)
    if tools_layer:
        layer.setdefault("TOOLS", {}).update(tools_layer)

    hooks_layer = _load_hooks_layer(root, suite_name=suite.name)
    if hooks_layer:
        layer["HOOKS"] = hooks_layer

    skills_layer = _load_skills_layer(root)
    if skills_layer:
        layer.setdefault("TOOLS", {}).setdefault("skills", {}).update(skills_layer)

    subagent_layer = _load_subagents_layer(root)
    if subagent_layer:
        layer["SUBAGENT_CONFIGS"] = subagent_layer

    actor_patches, node_unknown = _load_node_configs_layer(root, default_actor_nodes=default_actor_nodes)
    unknown_nodes.update(node_unknown)
    prompt_patches, prompt_unknown = _load_prompts_layer(root, default_actor_nodes=default_actor_nodes)
    unknown_nodes.update(prompt_unknown)

    actor_items = _merge_actor_patches(actor_patches, prompt_patches)
    if actor_items:
        layer["ACTOR_LOOP"] = actor_items

    return layer, unknown_nodes


def _load_tools_layer(root: Path) -> dict[str, Any]:
    """Load ``tools/tools.yaml`` TOOLS section."""
    tools_yaml = root / "tools" / "tools.yaml"
    if not tools_yaml.is_file():
        return {}
    with open(tools_yaml, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    if not isinstance(doc, Mapping):
        return {}
    tools = doc.get("TOOLS")
    return dict(tools) if isinstance(tools, Mapping) else {}


def _is_framework_hook_spec(spec: str) -> bool:
    """
    Return whether a Suite hook spec references a framework callable by absolute path.

    Framework paths (``dataagent.*``) are merged without a ``{suite_name}.`` prefix so
    runtime resolution reuses ``resolve_builtin_hook`` like user YAML entries.
    """
    return str(spec or "").strip().startswith("dataagent.")


def _load_hooks_layer(root: Path, *, suite_name: str) -> dict[str, Any]:
    """Load hooks and prefix suite-local callable paths with ``{suite_name}.``."""
    hooks_yaml = root / "hooks" / "hooks.yaml"
    if not hooks_yaml.is_file():
        return {}
    with open(hooks_yaml, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    if not isinstance(doc, Mapping):
        return {}
    hooks = doc.get("HOOKS")
    if not isinstance(hooks, Mapping):
        return {}
    return _prefix_hooks_dict(hooks, suite_name=suite_name)


def _prefix_hooks_dict(hooks: Mapping[str, Any], *, suite_name: str) -> dict[str, Any]:
    """Recursively prefix hook path strings in a HOOKS mapping."""
    result: dict[str, Any] = {}
    for key, value in hooks.items():
        if isinstance(value, list):
            result[key] = [_prefix_hook_item(item, suite_name=suite_name) for item in value]
        elif isinstance(value, Mapping):
            result[key] = _prefix_hooks_dict(value, suite_name=suite_name)
        else:
            result[key] = value
    return result


def _prefix_hook_item(item: Any, *, suite_name: str) -> Any:
    """
    Prefix one hook list entry with the Suite name.

    Suite-local specs (e.g. ``hooks.custom_hooks.audit_pre``) become
    ``{suite_name}.hooks.custom_hooks.audit_pre``. Framework specs starting with
    ``dataagent.`` are left unchanged.
    """
    if isinstance(item, str):
        rel = item.strip()
        if rel.startswith(f"{suite_name}.") or _is_framework_hook_spec(rel):
            return rel
        return f"{suite_name}.{rel}"
    if isinstance(item, Mapping):
        patched = dict(item)
        raw_name = str(patched.get("name") or "").strip()
        if raw_name and not raw_name.startswith(f"{suite_name}.") and not _is_framework_hook_spec(raw_name):
            patched["name"] = f"{suite_name}.{raw_name}"
        return patched
    return item


def _load_skills_layer(root: Path) -> dict[str, Any]:
    """
    Append Suite ``skills/`` to ``TOOLS.skills.custom_dirs`` (skills 目录并入 custom_dirs).

    Suite 不向 ``TOOLS.skills.builtin`` 写入 skill 名；合并后由 ``validate_unique_skill_names``
    校验将生效的 skill ``name`` 全局唯一。
    """
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return {}
    return {"custom_dirs": [str(skills_dir.resolve())]}


def _load_subagents_layer(root: Path) -> list[dict[str, str]]:
    """Load ``subagents/*.yaml`` as SUBAGENT_CONFIGS absolute paths."""
    subagents_dir = root / "subagents"
    if not subagents_dir.is_dir():
        return []
    entries: list[dict[str, str]] = []
    for item in sorted(subagents_dir.iterdir()):
        if not item.is_file():
            continue
        if item.suffix.lower() not in {".yaml", ".yml"}:
            continue
        entries.append({"path": str(item.resolve())})
    return entries


def _load_node_configs_layer(
    root: Path,
    *,
    default_actor_nodes: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Convert ``node_configs.yaml`` into ACTOR_LOOP list patches."""
    node_yaml = root / "node_configs.yaml"
    if not node_yaml.is_file():
        return [], set()
    with open(node_yaml, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    if not isinstance(doc, Mapping):
        return [], set()
    unknown: set[str] = set()
    patches: list[dict[str, Any]] = []
    for node_name, cfg in doc.items():
        node = str(node_name).strip()
        if node not in default_actor_nodes:
            unknown.add(node)
            continue
        if isinstance(cfg, Mapping):
            patch = {"node": node, **dict(cfg)}
            patches.append(patch)
    return patches, unknown


def _load_prompts_layer(
    root: Path,
    *,
    default_actor_nodes: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """
    Convert ``prompts/system`` and ``prompts/user`` into planner ACTOR_LOOP patches.

    Emit ``prompt_template`` list specs for planner; consumed by ``build_prompt_append``.
    """
    prompts_dir = root / "prompts"
    if not prompts_dir.is_dir():
        return [], set()
    if "planner" not in default_actor_nodes:
        return [], {"planner"}
    system_paths: list[dict[str, str]] = []
    user_paths: list[dict[str, str]] = []
    for sub, bucket in (("system", system_paths), ("user", user_paths)):
        subdir = prompts_dir / sub
        if not subdir.is_dir():
            continue
        for template in sorted(subdir.rglob("*")):
            if template.is_file() and template.suffix.lower() in {".md", ".j2", ".jinja", ".jinja2", ".txt"}:
                bucket.append({"path": str(template.resolve())})
    if not system_paths and not user_paths:
        return [], set()
    prompt_template: dict[str, list[dict[str, str]]] = {}
    if system_paths:
        prompt_template["system"] = system_paths
    if user_paths:
        prompt_template["user"] = user_paths
    return [{"node": "planner", "prompt_template": prompt_template}], set()


def _merge_actor_patches(*patch_groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Combine ACTOR_LOOP patch lists by node name."""
    by_node: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for group in patch_groups:
        for item in group:
            if not isinstance(item, Mapping):
                continue
            node = str(item.get("node") or "").strip()
            if not node:
                continue
            patch = dict(item)
            if node not in by_node:
                by_node[node] = patch
                order.append(node)
            else:
                existing = by_node[node]
                for key, value in patch.items():
                    if key == "node":
                        continue
                    if (
                        key == "prompt_template"
                        and isinstance(value, Mapping)
                        and isinstance(existing.get("prompt_template"), Mapping)
                    ):
                        merged_pt = dict(existing["prompt_template"])
                        for mt, specs in value.items():
                            if isinstance(specs, list):
                                merged_pt.setdefault(mt, [])
                                if isinstance(merged_pt[mt], list):
                                    merged_pt[mt] = list(specs) + list(merged_pt[mt])
                        existing["prompt_template"] = merged_pt
                    elif key not in existing:
                        existing[key] = value
                    else:
                        existing[key] = value
    return [by_node[node] for node in order]
