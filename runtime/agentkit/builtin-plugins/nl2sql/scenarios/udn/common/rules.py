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
"""Shared rule-injection texts for the UDN scenario packs.

Both packs append the same blocks to the SQL rules through the perceptor
hook's ``extra_rules``: the current date (business_twin adds the
relative-time parsing hint) and the time-granularity contract, applied when
the question explicitly names a granularity.
"""

from __future__ import annotations

from datetime import date


def current_date_rule(*, relative_time_hint: bool = False) -> str:
    """Rule text stating today's date; the hint teaches relative-time parsing."""
    today = date.today()
    text = f"\n现在为{today.year}年{today.month}月{today.day}日。"
    if relative_time_hint:
        text += "请基于当前日期解析用户问题中的相对时间。"
    return text


def time_granularity_rules() -> str:
    """Rule block enforcing the explicitly requested time granularity."""
    return (
        "\n\n## 时间粒度"
        "\n- 问题明确要求了时间粒度：`time` 必须出现在 SELECT 和 GROUP BY，结果每个时间桶一行。"
        "\n- 问题中出现的 统计 / 汇总 / 总计 等措辞不改变这一点，不得把结果压成单行。"
        "\n- 不得从 SELECT 或 GROUP BY 移除 `time`，任何要求移除 `time` 的意见都应判为无效。"
    )
