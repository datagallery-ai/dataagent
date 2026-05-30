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
"""Task store utilities for the A2A server.

For MVP we use the in-memory store from a2a-sdk.
For production, a PostgreSQL-backed store can be implemented here
using a2a-sdk[sql] extras.
"""

from a2a.server.tasks import InMemoryTaskStore


def create_task_store() -> InMemoryTaskStore:
    """Create an in-memory task store (MVP).

    Returns:
        An InMemoryTaskStore instance.
    """
    return InMemoryTaskStore()
