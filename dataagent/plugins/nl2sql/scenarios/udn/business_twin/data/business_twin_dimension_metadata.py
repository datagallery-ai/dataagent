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
"""business_twin dimension metadata: ``dimension_code`` bits → field labels.

Keys are the bit positions of the 16-hex-digit ``dimension_code`` embedded in
business-twin table names; ``name``/``description`` feed the table-family
selection prompt.
"""

BUSINESS_TWIN_DIMENSION_METADATA: dict[int, dict[str, str]] = {
    1: {
        "field": "mos4_qds",
        "name": "保障&MOS四象限",
        "description": "按保障状态与MOS质量优劣划分用户四象限",
    },
    2: {
        "field": "guarantee_group",
        "name": "保障分群",
        "description": "区分保障签约用户质差前、质差未保障、质差保障中和未签约用户",
    },
    3: {
        "field": "crh_group",
        "name": "高铁分群",
        "description": "按高铁场景和用户属性划分用户群",
    },
    4: {
        "field": "default5qi_group",
        "name": "默载5QI分群",
        "description": "按默认承载5QI值划分用户，例如5QI=6",
    },
    5: {
        "field": "term_brand",
        "name": "终端品牌分群",
        "description": "按苹果、华为、小米、荣耀、OPPO、VIVO等终端品牌划分用户",
    },
    6: {
        "field": "custom_group",
        "name": "自定义分群",
        "description": "按业务配置的自定义用户群划分用户",
    },
    7: {
        "field": "app_id",
        "name": "业务分类",
        "description": "业务大类，例如游戏、直播、即时通信、视频和办公",
    },
    8: {
        "field": "sub_app_id",
        "name": "应用",
        "description": "具体应用，例如抖音、快手、王者荣耀",
    },
    9: {
        "field": "supi",
        "name": "SUPI",
        "description": "用户永久标识，通常对应SUPI或IMSI",
    },
    10: {
        "field": "gpsi",
        "name": "GPSI",
        "description": "用户公共标识，通常对应包含国家码的手机号",
    },
    11: {
        "field": "tai",
        "name": "跟踪区",
        "description": "移动网络跟踪区标识TAI",
    },
    12: {
        "field": "gnb",
        "name": "基站标识",
        "description": "5G基站gNodeB标识",
    },
    13: {
        "field": "cell_id",
        "name": "小区标识",
        "description": "5G小区标识，用于各小区或指定小区分析",
    },
    14: {
        "field": "province",
        "name": "省份",
        "description": "省级行政区划",
    },
    15: {
        "field": "city",
        "name": "地市",
        "description": "地级市或直辖市行政区划，例如无锡市",
    },
    16: {
        "field": "county",
        "name": "区县",
        "description": "区县级行政区划，例如锡山区",
    },
    17: {
        "field": "cell_ul_group",
        "name": "小区上行PRB分群",
        "description": "按小区上行PRB负载程度划分小区",
    },
    18: {
        "field": "ne_name",
        "name": "网元名称",
        "description": "AMF、PCF或NWDAF等网元实例名称",
    },
    19: {
        "field": "cell_dl_group",
        "name": "小区下行PRB分群",
        "description": "按小区下行PRB负载程度划分小区",
    },
    20: {
        "field": "info_indicate",
        "name": "体验单据类型",
        "description": "区分保障用户EXP单据、体验用户EXP单据等体验信息单据类型",
    },
    21: {
        "field": "user_type",
        "name": "高铁用户类型",
        "description": "区分CRH高铁用户等高铁用户画像类型",
    },
    22: {
        "field": "crh_sub_ind",
        "name": "高铁签约标识",
        "description": "标识用户是否为高铁签约用户",
    },
    23: {
        "field": "crh_station_id",
        "name": "高铁站点",
        "description": "高铁站点标识",
    },
    24: {
        "field": "crh_section_id",
        "name": "高铁线路段",
        "description": "高铁线路段标识",
    },
    25: {
        "field": "crh_railway_id",
        "name": "高铁线路",
        "description": "高铁线路标识",
    },
    26: {
        "field": "most_resolution",
        "name": "最大占比分辨率",
        "description": "占比最高的分辨率",
    },
}
