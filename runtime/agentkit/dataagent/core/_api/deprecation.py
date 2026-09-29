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
"""把 langchain 的废弃警告再导出一层。

装饰器、抑制上下文和警告类用 langchain_core 的原对象。直接调用的
warn_deprecated 多一个 stacklevel，用来把警告指到调用方那一帧。
测试里用 reset_deprecation_dedupe 把「只警告一次」的标记清回去。
"""

from __future__ import annotations

import warnings

import langchain_core._api.deprecation as _lc_api

LangChainDeprecationWarning = _lc_api.LangChainDeprecationWarning
deprecated = _lc_api.deprecated
suppress_langchain_deprecation_warning = _lc_api.suppress_langchain_deprecation_warning
_langchain_warn = _lc_api.warn_deprecated

__all__ = ["LangChainDeprecationWarning", "deprecated", "reset_deprecation_dedupe", "suppress_langchain_deprecation_warning", "warn_deprecated"]

_WARNED_NAME = "warned"


def warn_deprecated(since: str, *, message: str = "", name: str = "", alternative: str = "", alternative_import: str = "", pending: bool = False, obj_type: str = "", addendum: str = "", removal: str = "", package: str = "", stacklevel: int = 2) -> None:
    """按 langchain 的句式发一条废弃警告。

    stacklevel 的数法与 warnings.warn 相同：1 是本函数的调用行。内部发出的
    第一条原样重发，空记录直接返回。message、pending、removal 保持原对象。
    """
    forwarded = {
        "message": message,
        "name": name,
        "alternative": alternative,
        "alternative_import": alternative_import,
        "pending": pending,
        "obj_type": obj_type,
        "addendum": addendum,
        "removal": removal,
        "package": package,
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _langchain_warn(since, **forwarded)
    if not caught:
        return
    notice = caught[0]
    warnings.warn(notice.message, category=notice.category, stacklevel=stacklevel + 1)


def _warned_cell(target: object):
    """找到闭包里名为 warned 的 cell。找不到就返回 None。"""
    subject = target.fget if isinstance(target, property) else target
    code_object = getattr(subject, "__code__", None)
    cells = getattr(subject, "__closure__", None)
    if code_object is None or cells is None:
        return None
    try:
        slot = code_object.co_freevars.index(_WARNED_NAME)
    except ValueError:
        return None
    return cells[slot]


def reset_deprecation_dedupe(*targets: object) -> None:
    """把装饰器闭包里的 warned 写回 False。

    只写 bool。property（含子类）才读 fget。__code__ 抛错时不再读
    __closure__，后面的目标也不再处理。
    """
    for target in targets:
        cell = _warned_cell(target)
        if cell is None:
            continue
        try:
            current = cell.cell_contents
        except ValueError:
            continue
        if isinstance(current, bool):
            cell.cell_contents = False
