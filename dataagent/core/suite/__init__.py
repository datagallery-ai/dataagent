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
"""Agent configuration Suite: discovery, merge, and validation."""

from dataagent.core.suite.activation import activate_suites, order_suites_for_merge
from dataagent.core.suite.allow_paths import effective_workspace_allow_paths
from dataagent.core.suite.discovery import discover_suite_index, scan_suite_paths
from dataagent.core.suite.merge import extract_user_layer, merge_layers
from dataagent.core.suite.suite_layer import build_suite_layers
from dataagent.core.suite.validation import validate_merged_config, validate_unique_skill_names

__all__ = [
    "effective_workspace_allow_paths",
    "activate_suites",
    "order_suites_for_merge",
    "build_suite_layers",
    "discover_suite_index",
    "extract_user_layer",
    "merge_layers",
    "scan_suite_paths",
    "validate_merged_config",
    "validate_unique_skill_names",
]
