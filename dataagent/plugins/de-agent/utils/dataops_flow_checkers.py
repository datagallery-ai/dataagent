# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""流程型数据正确性校验器的注册表。

两条流程型规则：

- ``common_sense`` —— 抽样行 + 字段语义，由模型判定跨字段的常识违规；
- ``source_reconciliation`` —— 抽样 3 行，按主键回源取数并逐字段比对。

它们与 :mod:`dataops_quality_check` 里的 ``QualityChecker`` 并列（见
:mod:`dataops_correctness_flow` 的模块说明）。
"""

from __future__ import annotations

from utils.dataops_common_sense import COMMON_SENSE_CHECKER
from utils.dataops_correctness_flow import FlowChecker
from utils.dataops_source_reconciliation import SOURCE_RECONCILIATION_CHECKER


def build_flow_checkers() -> tuple[FlowChecker, ...]:
    """返回全部流程型校验器。

    **延迟导入**：模块级导入会形成
    ``quality_check → flow_checkers → common_sense → quality_check`` 的环 ——
    用函数体内的导入打断它。
    """
    pass
    pass

    return (COMMON_SENSE_CHECKER, SOURCE_RECONCILIATION_CHECKER)


__all__ = ["build_flow_checkers"]
