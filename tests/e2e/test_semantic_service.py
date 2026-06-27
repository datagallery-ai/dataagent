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
from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path
from typing import Any

from loguru import logger

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))

from dataagent.interface.sdk.agent import DataAgent  # noqa: E402


async def main() -> bool:
    try:
        config_path = PROJECT_DIR / "dataagent" / "agents" / "semantic_service" / "semantic_service_test_agent.yaml"
        agent = DataAgent.from_config(config_path)

        query = (
            "请验证 semantic-service 的全部 semantic tools 链路。"
            "只需要调用工具并报告每个工具是否成功访问 semantic-service。"
            "工具返回文件路径时视为该工具已完成返回，不要读取、打开或解析任何文件路径。"
            "请依次调用以下工具："
            "1. list_semantic_layer_tables，列出语义层表清单；"
            "2. get_semantic_layer_table_schema，获取 data_table 的 schema；"
            "3. search_tables_and_columns，使用关键词 ['用户', '订单'] 检索表和列；"
            "4. search_tables_with_typename，使用关键词 '用户 订单' 检索表；"
            "5. get_table_schema，选择召回到的一张表获取 schema；"
            "6. get_join_relations，选择两张召回到的表查询 JOIN 关系；"
            "7. search_metric_instance，使用关键词 ['用户', '订单'] 检索指标；"
            "8. search_udf_function_by_name_keyword，使用关键词 'IsEmpty' 检索 UDF；"
            "9. search_udf_function_by_dsl，使用 function_description like '空' 检索 UDF。"
            "如果某个工具没有结果或调用失败，请在最终结果中标明工具名和失败原因。"
            "最终输出只需要按工具名列出成功/失败状态和简短原因，不要汇总文件内容。"
        )
        result: Any = await agent.chat(query, session_id=None)

        assert isinstance(result, dict), f"unexpected response type: {type(result)}"
        assert result.get("complete", False) is True, "semantic-service test agent did not complete"

        logger.info("semantic-service e2e completed.")
        return True
    except Exception as err:
        logger.error(f"semantic-service e2e failed: {err}\n{traceback.format_exc()}")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
