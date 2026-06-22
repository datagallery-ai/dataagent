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
from typing import Any


def get_metavisor_auth(config_manager: Any) -> tuple[str, str]:
    """Read MetaVisor Basic Auth credentials from agent configuration."""
    username = config_manager.get("METAVISOR.username")
    password = config_manager.get("METAVISOR.password")
    if not username or not password:
        raise ValueError("METAVISOR.username and METAVISOR.password must both be configured")
    return str(username), str(password)
