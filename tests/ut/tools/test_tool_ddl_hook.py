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

from loguru import logger

from dataagent.interface.sdk.agent import DataAgent


async def main():
    config_path = "dataagent/core/flex/examples/test_ddl_hook.yaml"
    agent = DataAgent.from_config(config_path)
    # 样例 DDL 缺少表级 COMMENT 关键字，字段 COMMENT 内容包含特殊字符 *
    query = """
    Write the DDL as the following example to the file named create_test_ddl.sql.

    CREATE TABLE ads_noah_ocpd_userfeat_user_second_type_usage_time_30d (
    oaid_sha256 STRING COMMENT '设备OAID的SHA256加密值',
    app_second_type STRING COMMENT '应用二级类型',
    user_use_total_mins_7d DECIMAL(26, 2) COMMENT '用户7天使用总时长（分钟）',
    user_use_total_mins_30d DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）',
    user_use_total_mins_30d_avg DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）平均值=user_use_total_mins_30d / 30',
    user_use_total_mins_30d_sum DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）总和',
    user_use_total_mins_30d_max DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）最大值',
    user_use_total_mins_30d_min DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）最小值',
    user_use_total_mins_30d_std DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）标准差',
    user_use_total_mins_30d_var DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）方差',
    user_use_total_mins_30d_skewness DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）偏度',
    user_use_total_mins_30d_kurtosis DECIMAL(26, 2) COMMENT '用户30天使用总时长（分钟）峰度',
    user_insert_score_7d DECIMAL(26, 2) COMMENT '用户7天二级分类兴趣得分=用户7天二级分类耗时占比*用户7天二级分类兴趣权重',
    user_insert_score_30d DECIMAL(26, 2) COMMENT '用户30天二级分类兴趣得分=用户30天二级分类耗时占比*用户30天二级分类兴趣权重'
)
PARTITIONED BY (pt_d string COMMENT '天分区')
STORED AS ORC;
    """
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
