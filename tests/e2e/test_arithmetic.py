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
import traceback
from pathlib import Path

from loguru import logger

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))
from dataagent.interface.sdk.agent import DataAgent  # noqa: E402


async def main():
    config_path = PROJECT_DIR / "dataagent/core/flex/examples/arithmetic.yaml"
    agent = DataAgent.from_config(config_path)
    query = "What is 5 + 3 * 2"
    try:
        response = await agent.chat(query)
        result = response["messages"][-1].content
        logger.info(result)
        return True
    except Exception as e:
        logger.error(f"❌ Workflow error: {e}\n{traceback.format_exc()}")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
