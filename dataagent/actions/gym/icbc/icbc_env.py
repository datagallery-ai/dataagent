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

import ast
import json
import os
from datetime import UTC, datetime, timedelta

import requests
from loguru import logger

from dataagent.actions.environment.env import Env
from dataagent.actions.skills.ontology_service.scripts.ontology_client import normalize_filter_dict

BASE_URL = os.getenv("ICBC_BASE_URL", "http://localhost:8000").rstrip("/")


def _safe_literal_eval(value):
    """Safely parse stringified literals; pass through other types unchanged."""
    if isinstance(value, str):
        try:
            return ast.literal_eval(value)
        except Exception:
            return value
    return value


def _pretty_json_for_display(x) -> str:
    """
    把 x 变成“带换行缩进”的 JSON 字符串：
    - x 是 dict/list：直接 dumps(indent=2)
    - x 是字符串：先 json.loads；失败则 ast.literal_eval（兼容 "{'a': 1}" 这种）
    - 其他：str(x)
    """
    if isinstance(x, (dict, list)):
        return json.dumps(x, ensure_ascii=False, indent=2)

    s = str(x)

    try:
        obj = json.loads(s)
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Processing pretty json Error: {e}")
        pass
    try:
        obj = ast.literal_eval(s)  # 兼容你贴的单引号 dict 字符串
        if isinstance(obj, (dict, list)):
            return json.dumps(obj, ensure_ascii=False, indent=2)
        return str(obj)
    except Exception:
        return s


class ICBCEnv(Env):
    """
    Environment providing basic arithmetic operations.

    This environment provides four fundamental mathematical operations:
    addition, subtraction, multiplication, and division.

    Example:
        >>> env = ArithmeticEnv()
        >>> env.tools['add'](5, 3)
        8
        >>> env.tools['multiply'](4, 7)
        28
        >>> env.tools['divide'](10, 2)
        5.0
    """

    def __init__(self):
        """
        Initialize the arithmetic environment.

        Args:
            precision: Number of decimal places to round results to (default: 2)
        """
        super().__init__()

    def init(self):
        pass

    @Env.tool
    def hop_search(self, uuid, hop_num, accurate_flag):
        """
        使用场景：
            - 需要查询多阶关系关联时使用，根据问题复杂度自行选择跳数
        函数功能:
            - 匹配从指定名称的起始节点 (start) 出发，经过指定跳数 (hop_num) 后到达的所有目标节点 (end) 之间的路径。
            - 根据 accurate_flag 参数控制跳数的匹配方式：
                - True: 精确匹配跳数（即路径边数必须等于 hop_num）
                - False: 匹配最多跳数（即路径边数 ≤ hop_num）
            - 自动添加过滤条件，排除起始节点和目标节点相同的情况（start <> end）
            - 限定起始节点的uuid为输入的 uuid
            - 返回起始节点名称、目标节点名称以及完整的路径信息
        Args:
            uuid (str): 起始节点的uuid，用于匹配 Cypher 查询中的 `start.uuid` 字段
            hop_num (int): 跳数（hop），表示路径的边数范围。具体行为由 `accurate_flag` 决定
            accurate_flag (bool): 控制跳数匹配的精确性：
                - True: 精确匹配跳数（路径边数 = hop_num）
                - False: 匹配最多跳数（路径边数 ≤ hop_num）
        """
        url = f"{BASE_URL}/hop_search"
        parameters = {"uuid": uuid, "hop_num": hop_num, "accurate_flag": accurate_flag}
        resp = requests.post(url, json=parameters)
        result = resp.json().get("result")
        if not result:
            return {
                "original_msg": "未查到相关多跳关联路径。",
                "frontend_msg": {
                    "left": (
                        "多跳路径关联查询流程\n"
                        "1. 接收参数：uuid（起始节点），hop_num（跳数），accurate_flag（精确标志）\n"
                        "2. 构造多阶关系跳数查询请求\n"
                        "3. 发送POST请求至 /hop_search 接口\n"
                        "4. 解析接口返回，未发现符合条件的多跳路径\n"
                        "5. 返回处理结果"
                    ),
                    "right": ("\n- 未查询到符合条件的多阶路径结果。\n"),
                },
            }
        return {
            "original_msg": resp.json()["result"],
            "frontend_msg": {
                "left": (
                    "# 多跳路径关联查询流程\n"
                    "1. 接收参数：uuid（起始节点），hop_num（跳数），accurate_flag（精确标志）\n"
                    "2. 构造多阶关系跳数查询请求\n"
                    "3. 发送POST请求至 /hop_search 接口\n"
                    "4. 解析接口返回，获取所有可达目标节点及路径\n"
                    "5. 返回处理结果"
                ),
                "right": (
                    "\n"
                    "- 已依据指定起点和跳数执行多阶路径搜索，输出如下：\n"
                    f"```\n{_pretty_json_for_display(resp.json()['result'])}\n```"
                ),
            },
        }

    @Env.tool
    def property_filter(self, element_class: str, element_type: str, filter_dict: dict) -> list[dict]:
        """
        使用场景：
            - 当需要从图数据库中检索满足特定属性条件的节点或边时使用，特别适用于：
                同一属性的多值筛选（如名称包含"红杉"或"高瓴"的基金）
                组合条件查询（如规模大于1亿且成立时间在2010年后）
                多关键词搜索（如描述中包含"人工智能"或"机器学习"的企业）
                范围条件组合（如年龄大于18且小于30的用户）
        函数功能:
            - 基础匹配 (MATCH):
            - 根据传入的 `element_type`（节点或边）和 `element_class`（节点标签或边类型）构建基础的 `MATCH` 子句。
            - 对于节点，生成 `MATCH (n:ElementClass)`。
            - 对于边，生成 `MATCH ()-[e:ElementClass]->()`。
            - 属性过滤 (WHERE):
            - 将 `filter_dict` 中的每个键值对转换为 Cypher 的属性过滤条件。
            - 键作为属性名，值作为比较逻辑（如 `> 100`, `CONTAINS 'text'`）。
            - 使用 `AND` 逻辑将所有过滤条件组合在一起，构成 `WHERE` 子句。
            - 如果 `filter_dict` 为空，则不生成 `WHERE` 子句。
            - 返回结果 (RETURN):
            - 生成一个基础的 `RETURN` 子句，用于返回匹配到的元素本身。
            - 返回 uuid, id, name，方便在查询结果中直接引用。
        Args:
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联。
                即e.start_id/e.end_id的来源是n.id，可以通过这种方式在filter_dict实现筛选过滤。
                注意！不要自己生成id，id应通过查询获得。
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            filter_dict (dict):
                一个字典，用于定义属性过滤条件。
                - **键 (Key)**: 字符串类型的属性名。
                - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                - 对于每个属性，在每个键值内尽量只生成一个条件
                    例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS_WITH 'A'"`, `"= 'Tencent'"`。
                - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                    例如：
                    正确示例为
                    - "CONTAINS 'A' OR CONTAINS 'B'"
                    - "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                    错误示例：
                    "CONTAINS 'A' OR 'B'"（错误原因：缺少操作符）"= '3' OR id = '9' OR id = '29'"
                    （错误原因：重复属性名id）
        """
        url = f"{BASE_URL}/property_filter"
        parsed_filter = _safe_literal_eval(filter_dict)
        parsed_filter = normalize_filter_dict(parsed_filter)

        parameters = {"element_class": element_class, "element_type": element_type, "filter_dict": parsed_filter}

        resp = requests.post(url, json=parameters)
        return {
            "original_msg": resp.json()["result"],
            "frontend_msg": {
                "left": (
                    f"""1. 接收参数：

参数说明

| 参数项 | 描述 | 值 |
|--------|------|----|
| **类名** | 处理的元素类别 | `{element_class}` |
| **节点/边类型** | 元素的具体类型 | `{element_type}` |
| **过滤条件** | 数据筛选条件 | `{filter_dict}` |

                    """
                ),
                "right": (
                    "### 执行结果\n"
                    "- 按输入条件已完成属性过滤检索，结果如下：\n"
                    f"```\n{_pretty_json_for_display(resp.json()['result'])}\n```"
                ),
            },
        }

    @Env.tool
    def pattern_search(self, path_pattern, return_vars):
        """
        使用场景：
            - 需要查询符合特定路径模式的实例时使用
        函数功能：
            - 根据指定的多跳路径模式执行图数据库查询
            - 通过解析节点和关系的连接模式，自动生成并执行Cypher查询，
            - 返回匹配路径中的所有节点或指定节点。
        Args:
            path_pattern (list): 路径模式定义列表，按顺序交替包含节点和关系定义
                格式: [
                    [node_label: str, props: dict],  # 节点定义(标签, 属性条件)
                    [rel_type: str, direction: str], # 关系定义(类型, 方向)
                    [node_label: str, props: dict],  # 下一节点
                    ...
                ]
                节点定义:
                    node_label - 节点标签(如"Person")
                    props - 一个属性条件字典，用于定义属性过滤条件。空字典{}表示无限制
                            - **键 (Key)**: 字符串类型的属性名。
                            - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                            - 对于每个属性，在每个键值内尽量只生成一个条件
                            例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS_WITH 'A'"`, `"= 'Tencent'"`。
                            - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                            例如：正确示例为
                            "CONTAINS 'A' OR CONTAINS 'B'"
                            "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                            错误示例：
                            "CONTAINS 'A' OR 'B'"（错误原因：缺少操作符）"= '3' OR id = '9' OR id = '29'"
                            （错误原因：重复属性名id）
                            注意！在生成props时，如果是CONTAINS条件，后面就不要再加 `'='`。
                关系定义:
                    rel_type - 关系类型(如"KpiOfOperatingIncome-RelatedTo-KpiOfNetFeeAndCommissionIncome")
                    direction - 方向指示符:
                        "-" 表示 (A)-(B)
                        "->" 表示 (A)->(B), A是起始节点，B是终止节点
                        "<-" 表示 (A)<-(B), B是起始节点，A是终止节点
                示例: [
                    ["Fund", {"name": " = 'A'"}],
                    ["Fund-INVESTS-Company", "-"],
                    ["Company", {}],
                    ["Company-OWNS-Person", "-"],
                    ["Person", {}]
                ]
                对应路径: (Fund)-[:Fund-INVESTS-Company]-(Company)-[:Company-OWNS-Person]-(Person)

            return_vars (list, optional): 指定返回的节点变量名列表
                默认: None (返回路径中所有节点)
                示例: ["var0", "var2"] 返回路径中第1个和第3个节点
                变量名按节点顺序自动分配:
                    第1个节点 -> "var0"
                    第2个节点 -> "var2"
                    第3个节点 -> "var3", 依此类推

        Returns:
            graphdb_result: 图数据库查询结果集，格式取决于底层驱动实现
            包含匹配路径中指定节点的数据
        """
        url = f"{BASE_URL}/pattern_search"

        parameters = {"path_pattern": _safe_literal_eval(path_pattern), "return_vars": _safe_literal_eval(return_vars)}
        resp = requests.post(url, json=parameters)
        return {
            "original_msg": resp.json()["result"],
            "frontend_msg": {
                "left": (
                    "# 路径模式查询流程\n"
                    "1. 接收 path_pattern（路径模式定义），return_vars（指定返回）参数\n"
                    "2. 构造多跳关联模式Cypher查询\n"
                    "3. 提交POST请求到 /pattern_search\n"
                    "4. 收集与模式匹配的节点及路径\n"
                    "5. 返回流程结果"
                ),
                "right": (
                    "### 执行结果\n"
                    "- 路径模式查询输出如下：\n"
                    f"```\n{_pretty_json_for_display(resp.json()['result'])}\n```"
                ),
            },
        }

    @Env.tool
    def property_info_search(self, element_class, element_type, element_uuid):
        """
        使用场景：
            - 需要查询某个节点或某条边上，所有的属性信息描述时使用。
              当你无法推断某个属性的语义信息，或需要确认某个属性代表的含义时，请调用此工具。
        函数功能：
            - 根据图元素类名element_class，图元素类型element_type，以及图元素element_uuid，
              返回该图元素上所有属性名，对应的属性语义描述，以及具体的属性取值
        Args:
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联，即e.start_id/e.end_id=n.id，
                可以通过这种方式在filter_dict实现筛选过滤
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            element_uuid：
                图元素中的uuid字段，用于定位具体图元素

        Returns:
            该函数返回某个节点或某条边的全部属性信息，以List[Dict[str, Any]]的格式，列表中包含多个字典，每个字典描述了一个属性的基本信息，
            分别是property_name属性命名，property_description属性含义，property_value属性取值。
            你可以根据property_description获取属性的语义信息，来解答用户提出的问题
            例如：对于查询，element_class='Fund', element_type='NODE', element_uuid='1'，返回如下结果
                [{"property_name": "fund_type", "property_description": "基金类型", "property_value": "产业投资基金"},
                    {"property_name": "AAA", "property_description": "基金的募集规模", "property_value": "80000.0"}]
                代表对于基金类型的节点1，有两个属性，
                分别是fund_type，含义是基金类型，取值是产业投资基金，
                AAA属性的含义是基金的募集规模，取值是80000.0
        """
        url = f"{BASE_URL}/property_info_search"
        parameters = {
            "element_class": element_class,
            "element_type": element_type,
            "element_uuid": element_uuid,
        }
        resp = requests.post(url, json=parameters)
        result = resp.json()["result"]
        result_display = result.copy()
        del result_display[0]["reminder"]
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": (
                    "# 属性信息检索流程\n"
                    "1. 接收 element_class、element_type、element_uuid\n"
                    "2. 组织查询请求，查询对应节点/边全量属性\n"
                    "3. 解析返回结构（属性名/属性含义/属性值）\n"
                    "4. 返回属性信息"
                ),
                "right": (
                    f"### 查询结果\n- 所有属性/含义/取值信息：\n```\n{_pretty_json_for_display(result_display)}\n```"
                ),
            },
        }

    @Env.tool
    def count_search(self, element_class, element_type, filter_dict):
        """
        使用场景：
            - 当需要从图数据库中统计满足特定属性条件的节点或边的数量时使用
        函数功能:
            - 基础匹配 (MATCH):
            - 根据传入的 `element_type`（节点或边）和 `element_class`（节点标签或边类型）构建基础的 `MATCH` 子句。
            - 对于节点，生成 `MATCH (n:ElementClass)`。
            - 对于边，生成 `MATCH ()-[e:ElementClass]->()`。
            - 属性过滤 (WHERE):
            - 将 `filter_dict` 中的每个键值对转换为 Cypher 的属性过滤条件。
            - 键作为属性名，值作为比较逻辑（如 `> 100`, `CONTAINS 'text'`）。
            - 使用 `AND` 逻辑将所有过滤条件组合在一起，构成 `WHERE` 子句。
            - 如果 `filter_dict` 为空，则不生成 `WHERE` 子句。
            - 返回结果 (RETURN):
            - 生成一个基础的 `RETURN` 子句，用于返回匹配到的元素本身。
            - 返回 满足filter_dict过滤条件的节点或边的数量。
        Args:
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联，即e.start_id/e.end_id的来源是n.id，
                可以通过这种方式在filter_dict实现筛选过滤。
                注意！不要自己生成id，id应通过查询获得。
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            filter_dict (dict):
                一个字典，用于定义属性过滤条件。
                - **键 (Key)**: 字符串类型的属性名。
                - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                - 对于每个属性，在每个键值内尽量只生成一个条件
                    例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS_WITH 'A'"`, `"= 'Tencent'"`。
                - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                    例如：正确示例为
                    - "CONTAINS 'A' OR CONTAINS 'B'"
                    - "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                    错误示例：
                    - "CONTAINS 'A' OR 'B'"
                    （错误原因：缺少操作符）
                    - "= '3' OR id = '9' OR id = '29'"
                    （错误原因：重复属性名id）
        """
        url = f"{BASE_URL}/count_search"
        parameters = {
            "element_class": element_class,
            "element_type": element_type,
            "filter_dict": _safe_literal_eval(filter_dict),
        }
        resp = requests.post(url, json=parameters)
        result = resp.json()["result"]
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": (
                    "# 节点/边数量统计流程\n"
                    "1. 接收类型和过滤条件\n"
                    "2. 构造数量统计请求\n"
                    "3. 提交POST到 /count_search\n"
                    "4. 解析数量结果\n"
                    "5. 输出统计信息"
                ),
                "right": (f"### 统计结果\n- 满足属性条件的数量：`{_pretty_json_for_display(result)}`"),
            },
        }

    @Env.tool
    def aggregate_search(self, element_class, element_type, target_property, agg, filter_dict):
        """
        使用场景：
            - 当需要对图数据库中特定类型的节点或边进行数值型属性的聚合统计时使用，典型场景包括：
            - 业务指标分析：计算基金节点的平均规模、统计投资边的总金额
            - 数据洞察：查询企业节点的最大注册资本、获取行业的最小成立年限
            - 条件统计：统计满足特定条件的节点数量（如名称包含"科技"的企业数量）
            - 趋势分析：按时间维度聚合交易数据（需配合时间属性）
        函数功能:
            - 基础匹配 (MATCH):
            - 根据传入的 `element_type`（节点或边）和 `element_class`（节点标签或边类型）构建基础的 `MATCH` 子句。
            - 对于节点，生成 `MATCH (n:ElementClass)`。
            - 对于边，生成 `MATCH ()-[e:ElementClass]->()`。
            - 属性过滤 (WHERE):
            - 将 `filter_dict` 中的每个键值对转换为 Cypher 的属性过滤条件。
            - 键作为属性名，值作为比较逻辑（如 `> 100`, `CONTAINS 'text'`）。
            - 使用 `AND` 逻辑将所有过滤条件组合在一起，构成 `WHERE` 子句。
            - 如果 `filter_dict` 为空，则不生成 `WHERE` 子句。
            - 返回结果 (RETURN):
            - 生成一个基础的 `RETURN` 子句，用于返回匹配到的元素本身。
            - 返回 满足filter_dict过滤条件的节点或边的数量。
        Args:
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联，即e.start_id/e.end_id的来源是n.id
                可以通过这种方式在filter_dict实现筛选过滤。
                注意！不要自己生成id，id应通过查询获得。
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            target_property（str）:
                要聚合的目标属性名
            agg（str）:
                聚合函数类型
                - 支持 "SUM", "AVG", "MIN", "MAX", "COUNT"
            filter_dict (dict):
                一个字典，用于定义属性过滤条件。
                - **键 (Key)**: 字符串类型的属性名。
                - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                - 对于每个属性，在每个键值内尽量只生成一个条件
                例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS_WITH 'A'"`, `"= 'Tencent'"`。
                - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                例如：正确示例为
                - "CONTAINS 'A' OR CONTAINS 'B'"
                - "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                错误示例："CONTAINS 'A' OR 'B'"（错误原因：缺少操作符）"= '3' OR id = '9' OR id = '29'"（错误原因：重复属性名id）
        Returns:
            聚合统计结果值
        """
        url = f"{BASE_URL}/aggregate_search"
        parameters = {
            "element_class": element_class,
            "element_type": element_type,
            "target_property": target_property,
            "agg": agg,
            "filter_dict": _safe_literal_eval(filter_dict),
        }
        resp = requests.post(url, json=parameters)
        result = resp.json()["result"]
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": (
                    "# 聚合统计流程\n"
                    "1. 接收聚合参数：目标类型/属性/方法/过滤条件\n"
                    "2. 组织Cypher聚合函数请求\n"
                    "3. POST到 /aggregate_search\n"
                    "4. 返回聚合结果"
                ),
                "right": (f"### 聚合结果\n- 聚合统计值：`{_pretty_json_for_display(result)}`"),
            },
        }

    @Env.tool
    def sorted_search(self, element_class, element_type, filter_dict, return_properties, sort_by, ascending):
        """
        使用场景：
            - 当需要对图数据库中特定类型的节点或边按照某个特定属性进行排序时使用
        函数功能:
            - 生成带条件过滤和排序的 Cypher 查询语句
            - 执行查询并返回满足以下条件的图数据结果：
            - 指定类型的节点或边（如 "Company" 节点或 "INVESTS" 关系）
            - 符合过滤条件的数据（如营收大于 10000）
            - 按指定属性排序（如按注册时间升序排列）
        Args:
            element_class (str):
                图元素的类名或标识符。
                - 如果 `element_type` 是 "NODE"，则此参数代表节点的 **标签** (Label)。
                例如 "Fund"。
                - 如果 `element_type` 是 "EDGE"，则此参数代表边的 **类型** (Type)。
                例如 "FundInvestment"。
                对于边，通常使用其中的"start_id"与"end_id"字段与节点关联，即e.start_id/e.end_id的来源是n.id。
                可以通过这种方式在filter_dict实现筛选过滤。
                注意！不要自己生成id，id应通过查询获得。
            element_type (str):
                指定要查询的图元素类型，必须是 "NODE" 或 "EDGE"。
                - 使用 "NODE" 查询节点。
                - 使用 "EDGE" 查询关系。
            filter_dict (dict):
                一个字典，用于定义属性过滤条件。
                - **键 (Key)**: 字符串类型的属性名。
                - **值 (Value)**: 字符串类型的 Cypher 比较或谓词逻辑。
                - 对于每个属性，在每个键值内尽量只生成一个条件
                    例如：`">= 10000"`, `"CONTAINS '中国'"`, `" STARTS_WITH 'A'"`, `"= 'Tencent'"`。
                - 如果要在子条件中使用OR语法，每个子条件必须是完整的比较表达式，但不需要每次都写出属性名
                    例如：正确示例为
                    - "CONTAINS 'A' OR CONTAINS 'B'"
                    - "= '清算期亏损基金' OR = '启明创投消费科技基金 IV' OR = '医疗设备并购基金' OR = '华芯科创创业投资基金'"
                    错误示例：
                    - "CONTAINS 'A' OR 'B'"（错误原因：缺少操作符）"= '3' OR id = '9' OR id = '29'"
                    （错误原因：重复属性名id）
            return_properties (list):
                - 需返回的属性列表，未提供时返回完整实体
                - 例：["name", "salary"]
            sort_by (str):
                - 排序依据的属性名，未提供时不排序
            ascending (bool):
                - 排序方向：True=升序(ASC)/False=降序(DESC)，默认 True
        Returns:
            当指定 return_properties 时：返回属性键值对列表
            例：[{"name": "腾讯", "revenue": 50000}, ...]
            未指定 return_properties 时：返回完整图元素对象（节点/边）
            结果按 sort_by 和 ascending 参数排序，无排序参数时按数据库默认顺序返回
        """
        url = f"{BASE_URL}/sorted_search"
        parameters = {
            "element_class": element_class,
            "element_type": element_type,
            "filter_dict": _safe_literal_eval(filter_dict),
            "return_properties": _safe_literal_eval(return_properties),
            "sort_by": sort_by,
            "ascending": ascending,
        }
        resp = requests.post(url, json=parameters)
        result = resp.json()["result"]
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": (
                    "# 排序查询流程\n"
                    "1. 接收类型、过滤条件、排序属性、方向等参数\n"
                    "2. 组织Cypher排序查询\n"
                    "3. POST到 /sorted_search\n"
                    "4. 解析排序结果"
                ),
                "right": (f"### 排序结果\n- 排序后数据如下：\n```\n{_pretty_json_for_display(result)}\n```"),
            },
        }

    def execute_action(self, element_class: str, element_uuid: str, action_name: str):
        """
        计算同比或环比数据

        Args:
            element_class
                - 对于边，通常通过 `start_id` 和 `end_id` 字段与节点关联，即 `e.start_id` 和 `e.end_id` 的来源是 `n.id`
                    可以通过这种方式在 `filter_dict` 中实现筛选过滤。
                - 注意：请勿自行生成 ID，ID 应通过查询获得。
            element_uuid: 指标节点的 UUID。
                例如 "KpiOfDailyCompanyDeposit" 或 "KpiOfDailyCompanyDeposit_0400000012#2#994#20251030"。
            action_name: 指定计算类型，支持以下两种：
                - "calculate_kpi_yoy_grouth"：计算同比数据
                - "calculate_kpi_mom_grouth"：计算环比数据

        Returns:
            dict: 包含计算结果的数据字典
        """
        url = f"{BASE_URL}/execute_action"
        parameters = {"element_class": element_class, "element_uuid": element_uuid, "action_name": action_name}
        resp = requests.post(url, json=parameters)
        result = json.dumps(resp.json()["result"], indent=2, ensure_ascii=False)
        return result

    @Env.tool
    def cal_mom(self, element_class: str, element_uuid: str):
        """
        计算环比数据

        Args:
            element_class
                - 对于边，通常通过 `start_id` 和 `end_id` 字段与节点关联，即 `e.start_id` 和 `e.end_id` 的来源是 `n.id`。
                    可以通过这种方式在 `filter_dict` 中实现筛选过滤。
                - 注意：请勿自行生成 ID，ID 应通过查询获得。
            element_uuid: 指标节点的 UUID。
                例如 "KpiOfDailyCompanyDeposit" 或 "KpiOfDailyCompanyDeposit_0400000012#2#994#20251030"。

        Returns:
            dict: 包含计算结果的数据字典
        """
        result = self.execute_action(element_class, element_uuid, "calculate_kpi_mom_growth")
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": ("# 指标环比计算流程\n1. 组装环比请求对应参数\n2. 内部调用 execute_action\n3. 返回环比结果"),
                "right": (f"### 计算结果\n- 指标环比结果如下：\n```\n{_pretty_json_for_display(result)}\n```"),
            },
        }

    @Env.tool
    def cal_yoy(self, element_class: str, element_uuid: str):
        """
        计算同比数据

        Args:
            element_class
                - 对于边，通常通过 `start_id` 和 `end_id` 字段与节点关联，即 `e.start_id` 和 `e.end_id` 的来源是 `n.id`。
                    可以通过这种方式在 `filter_dict` 中实现筛选过滤。
                - 注意：请勿自行生成 ID，ID 应通过查询获得。
            element_uuid: 指标节点的 UUID。
                例如 "KpiOfDailyCompanyDeposit" 或 "KpiOfDailyCompanyDeposit_0400000012#2#994#20251030"。

        Returns:
            dict: 包含计算结果的数据字典
        """
        result = self.execute_action(element_class, element_uuid, "calculate_kpi_yoy_growth")
        return {
            "original_msg": result,
            "frontend_msg": {
                "left": ("# 指标同比计算流程\n1. 组装同比请求对应参数\n2. 内部调用 execute_action\n3. 返回同比结果"),
                "right": (f"### 计算结果\n- 指标同比结果如下：\n```\n{_pretty_json_for_display(result)}\n```"),
            },
        }

    @Env.tool
    def get_bank_indicator_analysis_report_guidelines(self):
        """
        获取银行指标分析类的报告生成指南
        """
        return {
            "original_msg": """
# 要求:
- 只要涉及到指标查询，你就应该尝试给出当前指标的同比和环比数据。
- 对于具体数值，不要更改它的单位，按原样输出。

# 报告模板:
你要从以几个方面生成这个报告，首先根据用户输入，查询到目标机构，根据机构查询到报告需要分析的目标指标

1、总体趋势
在总体趋势中，你需要查询这个指标的当前指标值。并且从同比变化、环比变化、年度进度风险三个方面给出这个指标的情况。

2、结构分析
在这一章节中，你需要查询：
- 目标指标和哪些其他指标有关联。然后分析其他关联指标的情况。
- 给出关联指标的指标取值，通过查询关联指标的同比数据，给出关联指标的同比变化。
  通过查询关联指标的环比数据，给出关联指标的环比变化，目标机构的下辖机构的情况。
- 对目标机构的所有下辖机构，判断下辖机构是否存在目标指标的数据，如果存在，对下辖机构的目标指标，要给出指标取值
- 获得指标取值后，再查询下辖机构的目标指标的同比数据，给出同比变化，查询下辖机构的目标指标的环比数据，给出环比变化的分析。
- 不能仅仅只是获得指标取值就结束。

3、归因分析
在这一章节中，你需要：
- 从产品维度分析指标情况。找到和该指标相关的产品。
  对于每个产品，从指标值变化，当前指标值等维度，分析产品的风险点，为优化产品提出建议。
- 从客户维度分析指标情况。找到和该指标相关的客户。
  对于排名前3的客户，给出客户名称、时间范围、指标变化值、和当前指标值等情况，并根据这些情况进行特征分析，为优化客户贡献提出建议。

4、风险点分析
根据前三章的内容，对目标指标可能存在的风险给出分析。在生成分析时，可以引用之前查询获得的数据证明你的观点。

5、下一步方向
- 根据前三章的内容以及第4章给出的风险点，给出改善经营状况的建议。在生成建议时，可以引用之前查询获得的数据证明你的观点。
- 如果用户输入是历史对话摘要，你只需要查看还有哪些生成报告缺乏的信息并进行查询分析，不用再重复查询摘要中已经存在的信息。

        """,
            "frontend_msg": {
                "left": (
                    "获取银行指标分析报告指南流程\n"
                    # "1. 直接返回内置报告结构模板和编写指南内容"
                ),
                "right": ("### 报告模板与说明已载入"),
            },
        }

    @Env.tool
    def get_ontology_description(self):
        """
        获取和银行数据相关的本体描述
        """
        with open("dataagent/actions/gym/icbc/ontology_description.json", encoding="utf8") as fin:
            description = json.load(fin)
        return description

    @Env.tool
    def save_report_to_markdown(
        self,
        report_content: str,
        file_path: str = "",
    ) -> str:
        """
        使用场景：
            - 当 Agent 生成了一段较长的分析报告、结论说明或对账结果说明时。
                需要将结果长期保存到本地 Markdown 文件中，方便后续查阅、对比或接入其他系统。
            - 当用户要求生成报告时，最后一步**一定要**调用这个工具，将报告内容放在 `report_content` 参数中。
            - 如果用户没有要求生成报告，则不用调用该工具

        函数功能：
            - 将传入的 `report_content` 以 Markdown 格式写入到指定路径的 `.md` 文件中。
            - 如果未显式指定 `file_path`，则自动在项目根目录下的 `reports` 目录中按时间生成一个文件名。

        Args:
            report_content (str):
                - Agent 输出的完整报告内容，建议直接传入模型最终回答的文本。
            file_path (str, optional):
                - 目标 Markdown 文件的相对或绝对路径。
                - 支持传入已经存在的 `.md` 文件路径（会覆盖写入）或新文件路径。
                - 为空字符串或未提供时，默认路径为：
                  `<项目根目录>/reports/icbc_report_YYYYMMDD_HHMMSS.md`

        Returns:
            str:
                实际写入的 Markdown 文件的绝对路径。
        """
        # 计算默认保存路径：项目根目录下的 reports 目录
        project_root = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir))
        reports_dir = os.path.join(project_root, "reports")
        if not file_path:
            timestamp = (datetime.now(tz=UTC) + timedelta(hours=8)).strftime("%Y%m%d_%H%M%S")
            file_path = os.path.join(reports_dir, f"icbc_report_{timestamp}.md")
        elif not os.path.isabs(file_path):
            file_path = os.path.join(project_root, file_path)

        # 解析相对路径和符号链接，避免通过 ../ 或软链接逃逸。
        file_path = os.path.realpath(file_path)
        reports_dir = os.path.realpath(reports_dir)
        if os.path.commonpath([file_path, reports_dir]) != reports_dir:
            raise ValueError(f"file_path must be under reports directory: {reports_dir}")
        if not file_path.lower().endswith(".md"):
            raise ValueError("file_path must use the .md extension.")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        # 将内容写入 Markdown 文件，使用 UTF-8 编码，覆盖写入
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(report_content)

        logger.debug(f"Report saved to markdown file: {file_path}")
        return {
            "original_msg": os.path.abspath(file_path),
            "frontend_msg": {
                "left": (
                    "# 保存报告流程\n"
                    "1. 判断是否传入文件名\n"
                    "2. 若无则自动生成时间戳文件名\n"
                    "3. 确认目录存在，写入Markdown内容\n"
                    "4. 日志记录保存路径并返回"
                ),
                "right": (f"### 已保存Markdown报告\n- 文件路径：{os.path.abspath(file_path)}\n\n{report_content}"),
            },
        }
