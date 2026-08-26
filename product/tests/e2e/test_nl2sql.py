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
import asyncio
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))
from dataagent.interface.sdk.agent import DataAgent  # noqa: E402


async def main():
    config_path = PROJECT_DIR / "dataagent" / "agents" / "nl2sql" / "nl2sql_agent.yaml"
    agent = DataAgent.from_config(config_path)
    query = "Please list all the superpowers of 3-D Man."
    query += " 3-D Man refers to superhero_name = '3-D Man'; superpowers refers to power_name"
    await agent.chat(query)


if __name__ == "__main__":
    asyncio.run(main())
