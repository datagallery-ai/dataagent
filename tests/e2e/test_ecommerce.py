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
    config_path = PROJECT_DIR / "dataagent" / "core" / "flex" / "examples" / "ecommerce_agent.yaml"
    agent = DataAgent.from_config(config_path)
    # query = "分析这个句子的情绪：我家的猫真可爱。" # session_id="mycat"
    # query = "我刚刚说了什么？" # session_id="mycat"
    query = "请基于订单数据生成一份图文并茂的分析报告，按客户购买总金额排序，鉴别高购买力客户。"
    # query = "查询数据库并分析男女消费差异并生成图表"
    # query = "请基于订单数据生成一份图文并茂的收益分析报告，重点分析2023年9月1日的收益构成，深入剖析主要贡献收益的客户群体特征，特别是他们的职业分布情况。"
    result = await agent.chat(query, session_id=None)
    assert result["complete"] is True, "agent.chat() did not reach complete=True"
    logger.info(result)


if __name__ == "__main__":
    asyncio.run(main())
