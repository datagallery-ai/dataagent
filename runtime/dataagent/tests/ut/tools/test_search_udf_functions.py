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
from typing import cast

from dataagent.actions.tools.semantic_tool.search_udf_functions import _get_entity_by_guid
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient


def test_get_entity_by_guid_delegates_to_semantic_client() -> None:
    class _Client:
        def __init__(self) -> None:
            self.guids: list[str] = []

        def get_entity_by_guid(self, guid: str) -> dict:
            self.guids.append(guid)
            return {"entity": {"name": "udf"}}

    client = cast(SemanticServiceClient, _Client())

    assert _get_entity_by_guid("guid-1", client) == {"name": "udf"}
    assert client.guids == ["guid-1"]
