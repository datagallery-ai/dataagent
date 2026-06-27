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
"""Tests for effective workspace allow-path resolution."""

from dataagent.suite.allow_paths import effective_workspace_allow_paths
from dataagent.suite.validation import validate_merged_config
from dataagent.utils.runtime_paths import dataagent_package_path


def test_effective_allow_paths_merges_user_and_suite_roots() -> None:
    user_allow = "/tmp/user_extra"
    suite_root = str(dataagent_package_path("suite", "builtin_suites", "example_suite"))
    settings = {"WORKSPACE": {"allow_path": [user_allow]}}
    activated = [{"name": "example_suite", "root": suite_root}]
    paths = effective_workspace_allow_paths(settings, activated)
    assert paths[0] == user_allow
    assert any(p.endswith("example_suite") for p in paths)


def test_effective_allow_paths_dedupes_suite_root() -> None:
    suite_root = str(dataagent_package_path("suite", "builtin_suites", "example_suite"))
    settings: dict = {}
    activated = [{"name": "example_suite", "root": suite_root}]
    first = effective_workspace_allow_paths(settings, activated)
    second = effective_workspace_allow_paths(settings, activated)
    assert first == second
    assert len(first) == 1


def test_validate_subagent_path_allowed_by_activated_suite_root() -> None:
    suite_root = dataagent_package_path("suite", "builtin_suites", "example_suite")
    subagent = suite_root / "subagents" / "arithmetic_ref.yaml"
    config = {"SUBAGENT_CONFIGS": [{"path": str(subagent)}]}
    activated = [{"name": "example_suite", "root": str(suite_root)}]
    validate_merged_config(config, activated_suites=activated)
