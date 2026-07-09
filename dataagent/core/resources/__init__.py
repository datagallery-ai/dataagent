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
"""ResourceLoader: registry, operations, and job-backed resource execution (Phase B)."""

from dataagent.core.resources.models import Resource
from dataagent.core.resources.registry import ResourceRegistry, validate_resources_list
from dataagent.core.resources.service import ResourceService

__all__ = [
    "Resource",
    "ResourceRegistry",
    "ResourceService",
    "validate_resources_list",
]
