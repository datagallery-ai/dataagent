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
# 本文件中的模型可见文案常量原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""画像排除中间件：校验、过滤、覆盖率。

调用方把一条画像上的 ``excluded_middleware`` 交过来。这里做三件事：

1. 排除项若点到骨架类或骨架名字，立刻报错。骨架名单不在本文件，由参数传入。
2. 从已经装好的一条栈里摘掉匹配项。类按精确类型，名字按 ``.name``。
3. 几条栈都滤完之后，检查每条排除项至少命中过一次。

命中骨架时才去加载拒绝文案的格式化函数。那个模块可以还不存在，本文件仍然能被导入。
"""

import logging

logger = logging.getLogger(__name__)

# 下面五段是解析器折好的完整字面量。插值留在函数里，不要写进常量。
_COLLISION_HEAD = (
    "HarnessProfile.excluded_middleware name entry matched multiple distinct middleware classes within a single stack: "
)
_COLLISION_TAIL = ". Use a class-form exclusion via the runtime `HarnessProfile` to disambiguate."
_COVERAGE_HEAD = "HarnessProfile.excluded_middleware entries matched no middleware across any assembled stack: "
_COVERAGE_TAIL = (
    ". Typo or stale profile — every exclusion must correspond to a middleware actually "
    "present at runtime. (Tip: use class-form exclusion when the class is available to catch typos at import time.)"
)
_DROPPED_LOG = "Dropped %d middleware instance(s) from stack per profile.excluded_middleware=%r (matched classes=%s, names=%s)"


def _classify_exclusions(entries):
    """类对象进类集合，其余（含字符串）进名字集合。"""
    classes: set[type] = set()
    names: set[object] = set()
    for entry in entries:
        bucket = classes if isinstance(entry, type) else names
        bucket.add(entry)
    return classes, names


def _validate_excluded_middleware_config(profile, *, required_classes, required_names):
    """排除项碰到骨架才报错。空容器直接返回，并且不去加载格式化函数。"""
    selection = profile.excluded_middleware
    if not selection:
        return

    classes, names = _classify_exclusions(selection)
    forbidden_classes = classes & required_classes
    forbidden_names = names & required_names
    if not forbidden_classes and not forbidden_names:
        return

    # 只有这条路径才 import。句子由对方拼，本文件不写那句拒绝文案。
    from dataagent.core.profiles.harness.harness_profiles import _format_scaffolding_rejection

    labels = [cls.__name__ for cls in forbidden_classes]
    labels += [f"{name!r} (string)" for name in forbidden_names]
    raise ValueError(_format_scaffolding_rejection(labels))


def _raise_on_name_collisions(name_matched_types):
    """同一个名字在这一次过滤里命中了两个以上的类，就整段报出来。"""
    clashes = {name: found for name, found in name_matched_types.items() if len(found) > 1}
    if not clashes:
        return

    labels = sorted(
        f"{name!r} matched {sorted(cls.__name__ for cls in found)}" for name, found in clashes.items()
    )
    raise ValueError(f"{_COLLISION_HEAD}{'; '.join(labels)}{_COLLISION_TAIL}")


def _apply_excluded_middleware(stack, profile, *, matched_classes=None, matched_names=None):
    """按画像从一条栈里摘中间件。返回新列表；冲突时不回滚已经记下的命中。"""
    selection = profile.excluded_middleware
    if not selection:
        return list(stack)

    classes, names = _classify_exclusions(selection)
    kept = []
    name_to_types: dict[object, set[type]] = {}
    for item in stack:
        kind = type(item)
        # 类排除也要先读 name，读失败就让异常原样出去。
        item_name = item.name
        if kind in classes:
            if matched_classes is not None:
                matched_classes.add(kind)
            continue
        if item_name in names:
            name_to_types.setdefault(item_name, set()).add(kind)
            if matched_names is not None:
                matched_names.add(item_name)
            continue
        kept.append(item)

    _raise_on_name_collisions(name_to_types)

    # len 打在原对象上。有排除项时生成器会在这里 TypeError，不要先收成列表。
    removed = len(stack) - len(kept)
    if removed:
        logger.debug(
            _DROPPED_LOG,
            removed,
            sorted(repr(entry) for entry in profile.excluded_middleware),
            sorted(cls.__name__ for cls in classes),
            sorted(names),
        )
    return kept


def _verify_excluded_middleware_coverage(
    profile,
    matched_classes,
    matched_names,
    *,
    required_classes,
    required_names,
):
    """每条排除项都要在某条栈上命中过。骨架和下划线名字不算缺失。"""
    selection = profile.excluded_middleware
    if not selection:
        return

    classes, names = _classify_exclusions(selection)
    missing_classes = classes - matched_classes - required_classes
    missing_names = names - matched_names - required_names
    missing_names = {name for name in missing_names if not name.startswith("_")}
    if not missing_classes and not missing_names:
        return

    labels = sorted({cls.__name__ for cls in missing_classes} | {f"{name!r} (string)" for name in missing_names})
    raise ValueError(f"{_COVERAGE_HEAD}{', '.join(labels)}{_COVERAGE_TAIL}")
