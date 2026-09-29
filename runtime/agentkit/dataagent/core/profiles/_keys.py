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
"""画像注册表键的形状检查。

注册进去的键只有两种合法样子：单独一个 provider，或者第一个半角冒号
左边是 provider、右边整段是 model。model 里可以再出现冒号，这里不拆开，
也不把键折成小写或去掉空白。通过时返回 None。
"""

from __future__ import annotations

_EMPTY_KEY = "empty"
_OUTER_SPACE = "outer"
_BLANK_HALF = "blank-half"
_COLON_SPACE = "colon-space"


def validate_profile_key(key: str) -> None:
    """校验一个注册表键。合法则返回 None，否则抛 ``ValueError``。"""
    reason = _problem(key)
    if reason is None:
        return
    raise ValueError(_message(key, reason))


def _problem(key):
    """按固定顺序给出拒绝原因。顺序变了，抛出的句子也会变。"""
    if not key:
        return _EMPTY_KEY
    if _outer_whitespace(key):
        return _OUTER_SPACE
    if ":" not in key:
        return None
    provider, model = _provider_and_model(key)
    if not provider or not model:
        return _BLANK_HALF
    if provider != provider.strip() or model != model.strip():
        return _COLON_SPACE
    return None


def _outer_whitespace(text):
    return text != text.strip()


def _provider_and_model(text):
    provider, _separator, model = text.partition(":")
    return provider, model


def _message(key, reason):
    if reason == _EMPTY_KEY:
        return "Profile key must be a non-empty string."
    if reason == _OUTER_SPACE:
        return f"Profile key {key!r} has leading or trailing whitespace; expected 'provider' or 'provider:model'."
    if reason == _BLANK_HALF:
        return f"Profile key {key!r} has an empty provider or model half; expected 'provider:model'."
    return f"Profile key {key!r} has whitespace adjacent to ':'; expected 'provider:model' with no spaces around the first ':'."
