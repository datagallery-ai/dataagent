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
import requests


def get_table_description(table_name: str, base_url: str, auth: tuple) -> str:
    """
    根据表名查询 data_table 详情，并提取 table_description。

    Args:
        table_name: 表 qualified_name
        base_url: API基础URL
        auth: 认证元组

    Returns:
        str: 表描述。接口没有返回 table_description 时返回空字符串。
    """
    table_detail_url = f"{base_url}/api/metaVisor/v3/entity/uniqueAttribute/type/data_table"
    params = {"attr:qualified_name": table_name}
    table_detail = requests.get(table_detail_url, params=params, auth=auth).json()
    entity = table_detail.get("entity", {})
    attributes = entity.get("attributes", {})
    table_description = attributes.get("table_description") or entity.get("table_description") or ""

    return str(table_description)
