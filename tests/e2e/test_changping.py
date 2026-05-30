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

from loguru import logger

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))
from dataagent.interface.sdk.agent import DataAgent  # noqa: E402


async def main():
    config_path = PROJECT_DIR / "dataagent" / "core" / "flex" / "examples" / "changping.yaml"
    print(config_path)
    agent = DataAgent.from_config(config_path)

    query_list = [
        # 报告生成类
        "安排BD55-1111, XBB.1.5, 和huh-7的中和实验"
    ]
    response = await agent.chat(query_list[0], session_id=None)
    logger.info(response)


if __name__ == "__main__":
    asyncio.run(main())
