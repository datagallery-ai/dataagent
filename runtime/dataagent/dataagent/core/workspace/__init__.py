# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Workspace catalog frontmatter for Flex subagent directories."""

from dataagent.core.workspace.catalog import (
    append_job,
    catalog_path,
    inspect_environment,
    list_environments,
    load_catalog,
    refresh_artifacts,
    register_environment,
    safe_append_job,
    safe_refresh_artifacts,
    safe_register_environment,
    safe_touch_catalog,
    save_catalog,
    scan_artifacts,
    touch_catalog,
)
from dataagent.core.workspace.frontmatter import JobSummary, SubagentWorkspaceEntry, WorkspaceCatalogDoc
from dataagent.core.workspace.publish import (
    ensure_subagent_output_root,
    list_published_artifacts,
    load_publish_manifest,
    publish_subagent_artifacts,
)

__all__ = [
    "JobSummary",
    "SubagentWorkspaceEntry",
    "WorkspaceCatalogDoc",
    "append_job",
    "catalog_path",
    "inspect_environment",
    "list_environments",
    "load_catalog",
    "refresh_artifacts",
    "register_environment",
    "safe_append_job",
    "safe_refresh_artifacts",
    "safe_register_environment",
    "safe_touch_catalog",
    "save_catalog",
    "scan_artifacts",
    "touch_catalog",
    "ensure_subagent_output_root",
    "list_published_artifacts",
    "load_publish_manifest",
    "publish_subagent_artifacts",
]
