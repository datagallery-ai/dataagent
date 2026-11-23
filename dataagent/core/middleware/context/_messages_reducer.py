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
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""messages 通道的增量归并。

DeltaChannel 每次送来两样东西：已经积下来的消息，以及这一步新写进来的一批。
这里把新写的摊平、接上旧的，再按 id 覆盖或删除。

id 为空的消息只往后排，不在这里补编号。流式 chunk 保持原类，不收成完整消息。
清空哨兵只认写入里的 RemoveMessage，不认旧列表里碰巧带着同一个 id 的普通消息。
"""

from langchain_core import messages as lc_messages
from langgraph.graph import message as lg_message


def _spread(batch):
    """一层展开。只有 list（含子类）拆开，tuple 和字符串都算一条。"""
    pieces = []
    for item in batch:
        if isinstance(item, list):
            pieces.extend(item)
        else:
            pieces.append(item)
    return pieces


def _accept_prior(state):
    """旧列表：开头已经是消息就原样用，否则交给转换函数。

    用真值判断，不用 `is not None`。假值（含空串、0、False、空列表）都当成空列表。
    为真但不支持下标的对象，让下标错误自己抛出去。
    """
    if state and isinstance(state[0], lc_messages.BaseMessage):
        return state
    return lc_messages.convert_to_messages(state or [])


def _after_last_reset(incoming):
    """找到最后一处清空哨兵。返回哨兵之后的写入，以及是否清过。"""
    cut = None
    sentinel = lg_message.REMOVE_ALL_MESSAGES
    for index, item in enumerate(incoming):
        if isinstance(item, lc_messages.RemoveMessage) and item.id == sentinel:
            cut = index
    if cut is None:
        return incoming, False
    return incoming[cut + 1 :], True


def _remember(slots, located, item):
    """放进槽位，并让索引指向这条 id 的最后一次出现。"""
    key = item.id
    if key is not None:
        located[key] = len(slots)
    slots.append(item)


def _apply_one(slots, located, item):
    """按「无 id → 删除 → 替换 → 追加」的顺序处理一条写入。"""
    key = item.id
    if key is None:
        slots.append(item)
        return
    if isinstance(item, lc_messages.RemoveMessage):
        at = located.pop(key, None)
        if at is not None:
            slots[at] = None
        return
    at = located.get(key)
    if at is None:
        located[key] = len(slots)
        slots.append(item)
        return
    slots[at] = item


def _combine(prior, incoming):
    """合并。洞先留成 None，最后再滤掉，避免把空内容的消息一起丢掉。"""
    slots = []
    located = {}
    for item in prior:
        _remember(slots, located, item)
    for item in incoming:
        _apply_one(slots, located, item)
    kept = []
    for item in slots:
        if item is not None:
            kept.append(item)
    return kept


def _messages_delta_reducer(state, writes):
    """把这一批写入并进已有消息。

    `state` 可以是 None，表示这条通道还没有种过空列表。
    `writes` 的每个元素要么是一列消息，要么是单独的一条。
    返回新的 list，不改调用方手里的那个容器。
    """
    flat = _spread(writes)
    prior = _accept_prior(state)
    incoming = lc_messages.convert_to_messages(flat)
    kept, cleared = _after_last_reset(incoming)
    if cleared:
        prior = []
    return _combine(prior, kept)
