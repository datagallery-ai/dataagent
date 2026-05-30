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
    config_path = PROJECT_DIR / "dataagent" / "core" / "flex" / "examples" / "icbc.yaml"
    save_dir = "./DataAgent"
    print(config_path)
    agent = DataAgent.from_config(config_path)

    query_list = [
        # 报告生成类
        f"请你分析深圳蛇口支行各项存款的指标情况,存在放在{save_dir}/deepseek_r1_report_1.md",
        f"帮我生成一份深圳蛇口支行在2025年10月30日的本外币公司存款日均余额分析报告,存在放在{save_dir}/deepseek_r1_report_2.md",
        f"请你分析蛇口支行各项贷款的指标情况,存在放在{save_dir}/deepseek_r1_report_3.md",
        f"请你分析深圳蛇口支行营业收入的指标情况,存在放在{save_dir}/report/deepseek_r1_report_4.md",
        # 灵活分析类
        "帮我查询一下本外币公司的存款日均余额有哪几个子指标？深圳蛇口支行在2025年10月30日这些子指标分别是多少",
        "帮我查询下深圳蛇口支行的本外币公司存款日均余额指标在2024年10月的平均值是多少？",
        "深圳蛇口支行2025年10月30日的定期存款日均余额是否多于获取存款日均余额？",
    ]
    # query = "帮我测试一下test_hop_search"
    # query = "解释一下机器学习的概念"
    response = await agent.chat(query_list[3], session_id=None)
    # result = response.get("final_answer", response)
    logger.info(response)


if __name__ == "__main__":
    asyncio.run(main())
