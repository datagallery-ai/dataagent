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
"""Template loading and non-blocking compatibility helpers for the DataTaskIR spike."""

# ruff: noqa: UP045 -- repository convention requires Optional for nullable annotations.

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_TEMPLATE_PATH = Path(__file__).with_name("templates.yaml")


def load_template_catalog(path: Optional[Path] = None) -> dict[str, Any]:
    """Load and minimally validate the self-describing DataTaskIR template catalog."""
    template_path = path or DEFAULT_TEMPLATE_PATH
    loaded = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("DataTaskIR template catalog must be a mapping")
    fields = loaded.get("fields", [])
    if not isinstance(fields, list) or not fields:
        raise ValueError("DataTaskIR template catalog must contain non-empty fields")
    field_ids = []
    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("Every DataTaskIR field template must be a mapping")
        field_id = field.get("field_id")
        if not isinstance(field_id, str) or not field_id:
            raise ValueError("Every DataTaskIR field template must have a field_id")
        if field_id in field_ids:
            raise ValueError(f"Duplicate DataTaskIR field_id: {field_id}")
        field_ids.append(field_id)
        _validate_field_template(field)
    return loaded


def list_field_templates(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    """Return independent field units enriched with the catalog-level contract."""
    contract = deepcopy(catalog.get("contract", []))
    output_contract = deepcopy(catalog.get("output_contract", {}))
    version = catalog.get("template_version")
    templates = []
    for original in catalog.get("fields", []):
        field = deepcopy(original)
        field["template_contract"] = contract
        field["output_contract"] = output_contract
        field["template_version"] = version
        templates.append(field)
    return templates


def get_field_template(catalog: dict[str, Any], field_id: str) -> dict[str, Any]:
    """Return one independent field unit by identifier."""
    for field in list_field_templates(catalog):
        if field.get("field_id") == field_id:
            return field
    raise KeyError(f"Unknown DataTaskIR field_id: {field_id}")


def materialize_writable_template(
    field_template: dict[str, Any], current_value: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """Copy a field's writable template and optionally prefill its current value."""
    writable = deepcopy(field_template.get("writable_template", {}))
    if not isinstance(writable, dict):
        raise ValueError("writable_template must be a mapping")
    if current_value is None:
        return writable
    candidate_value = current_value.get("value", current_value)
    writable["value"] = deepcopy(candidate_value)
    return writable


def build_output_schema(field_template: dict[str, Any]) -> dict[str, Any]:
    """Return the legacy descriptive schema without enforcing it in the fill pipeline."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["field_id", "value"],
        "properties": {
            "field_id": {"const": field_template.get("field_id")},
            "value": deepcopy(field_template.get("value_schema", {})),
            "unresolved": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "question": {"type": "string", "minLength": 1},
        },
    }


def validate_field_output(field_template: dict[str, Any], output: dict[str, Any]) -> None:
    """Retain the legacy public function as a non-blocking compatibility no-op."""
    del field_template, output


def _validate_field_template(field: dict[str, Any]) -> None:
    required = ["field_id", "title", "purpose", "field_instructions", "few_shots", "writable_template"]
    missing = [name for name in required if field.get(name) is None]
    if missing:
        field_id = field.get("field_id", "<unknown>")
        raise ValueError(f"Field template {field_id} is missing: {', '.join(missing)}")
    writable = field.get("writable_template", {})
    if not isinstance(writable, dict) or writable.get("field_id") != field.get("field_id"):
        raise ValueError(f"Field template {field.get('field_id')} has an invalid writable_template")
    if writable.get("value") is None:
        raise ValueError(f"Field template {field.get('field_id')} must expose a writable value")
