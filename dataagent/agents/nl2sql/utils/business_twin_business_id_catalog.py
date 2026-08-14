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

import json
from typing import Any

from dataagent.utils.runtime_paths import dataagent_package_path

_CATALOG_PATH = dataagent_package_path(
    "agents",
    "nl2sql",
    "utils",
    "business_twin_business_id_catalog.json",
)


def _load_catalog() -> dict[str, Any]:
    """Load and validate the packaged business-twin table-selection catalog."""
    payload = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported business-twin business ID catalog version")
    for field in (
        "ignored_temporal_fields",
        "network_subject_terms",
        "business_schemas",
        "business_rules",
    ):
        if field not in payload:
            raise ValueError(f"business-twin business ID catalog is missing {field!r}")
    return payload


_CATALOG = _load_catalog()
_IGNORED_TEMPORAL_FIELDS = frozenset(_CATALOG["ignored_temporal_fields"])
_NETWORK_SUBJECT_TERMS = dict(_CATALOG["network_subject_terms"])
_BUSINESS_SCHEMAS = {
    business_id: {
        "metrics": frozenset(schema["metrics"]),
        "dimensions": frozenset(schema["dimensions"]),
    }
    for business_id, schema in _CATALOG["business_schemas"].items()
}
_METRIC_FIELDS = frozenset(field for schema in _BUSINESS_SCHEMAS.values() for field in schema["metrics"])
_DIMENSION_FIELDS = frozenset(field for schema in _BUSINESS_SCHEMAS.values() for field in schema["dimensions"])
_BUSINESS_RULES = dict(_CATALOG["business_rules"])
del _CATALOG
