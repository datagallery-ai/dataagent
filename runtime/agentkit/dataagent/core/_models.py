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
"""把模型规格解析成聊天模型，并回答「这是哪个模型、哪家 provider、是不是 Bedrock」。

字符串规格会先解析全局 ``init_chat_model``，再问 provider 画像要构造参数，
然后把画像的返回值展开交给刚才解析到的那个函数。
已经构造好的 ``BaseChatModel`` 原样返回，不再套一层画像。
"""

import logging
from collections.abc import Mapping

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from dataagent.core.profiles.provider.provider_profiles import apply_provider_profile

logger = logging.getLogger(__name__)

# 日志格式串是行为契约（附录 §10）。提到模块级，避免和调用语句粘成同一块。
_LS_PARAMS_FAILED = "Could not extract provider from %s.%s via _get_ls_params: %s"
_LS_PARAMS_NOT_MAPPING = "Could not extract provider from %s.%s: _get_ls_params returned %s, not a mapping"
_PROVIDER_UNVERIFIED = (
    "Matched spec %r on identifier alone; provider for %s.%s is uninspectable, so the spec's %r provider was not verified"
)

_BEDROCK_PROVIDERS = frozenset(
    (
        "amazon_bedrock",
        "anthropic_bedrock",
        "aws",
        "bedrock",
        "bedrock_converse",
    )
)
_BEDROCK_CLASS_NAMES = frozenset(
    (
        "ChatAnthropicBedrock",
        "ChatBedrock",
        "ChatBedrockConverse",
        "ChatBedrockNovaSonic",
    )
)
# 只剥掉第一个命中的前缀。``us.`` 与 ``us-gov.`` 的第三个字符不同，顺序因此安全。
_REGION_PREFIXES = (
    "apac.",
    "amer.",
    "au.",
    "eu.",
    "global.",
    "jp.",
    "sa.",
    "us.",
    "us-gov.",
)


# 别名用字典 get。``==`` 会在哈希对不上时仍去调用 ``__eq__``，和上游不一致。
_PROVIDER_ALIASES = {
    "azure_openai": "azure",
    "mistralai": "mistral",
}


def _fold_provider(name: str) -> str:
    """大小写与连字符折叠后，用字典 ``get`` 收两个已知别名。"""
    normalized = name.lower().replace("-", "_")
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _nonempty_str(owner: object, field: str) -> str | None:
    """读一个字符串属性。没有、真值为假、非字符串都当没有；``AttributeError`` 也当没有。"""
    found = getattr(owner, field, None)
    if isinstance(found, str) and found:
        return found
    return None


def _drop_one_region_prefix(text: str) -> str:
    for prefix in _REGION_PREFIXES:
        if text.startswith(prefix):
            return text.removeprefix(prefix)
    return text


def _string_targets_bedrock(text: str) -> bool:
    bare = _drop_one_region_prefix(text)
    if bare.startswith("amazon.nova-"):
        return True
    head, colon, _rest = text.partition(":")
    return bool(colon) and _fold_provider(head) in _BEDROCK_PROVIDERS


def get_model_identifier(model: BaseChatModel) -> str | None:
    """先看 ``model_name``，没有再用 ``model``。空白字符串算有值，真值为假的不算。

    内层已经求过一次真值。外层 ``or`` 再求一次：第二次为假就改读 ``model``，第二次抛异常就穿透。
    """
    return _nonempty_str(model, "model_name") or _nonempty_str(model, "model")


def get_model_provider(model: BaseChatModel) -> str | None:
    """从 ``_get_ls_params()['ls_provider']`` 取 provider，原样返回，不折叠。

    只接住 ``AttributeError``、``TypeError``、``NotImplementedError``。
    返回值不是 Mapping 时也不去调用 ``get``。
    类型在写日志时才取，调用 ``_get_ls_params`` 之前不读 ``type``。
    """
    try:
        payload = model._get_ls_params()
    except (AttributeError, TypeError, NotImplementedError) as exc:
        logger.info(_LS_PARAMS_FAILED, type(model).__module__, type(model).__name__, exc)
        return None
    if isinstance(payload, Mapping):
        provider = payload.get("ls_provider")
        if isinstance(provider, str) and provider:
            return provider
        return None
    logger.info(
        _LS_PARAMS_NOT_MAPPING,
        type(model).__module__,
        type(model).__name__,
        type(payload).__name__,
    )
    return None


def is_bedrock_model(model: str | BaseChatModel) -> bool:
    """字符串走 Nova / provider 前缀；其他对象先看 provider，对不上再看类名。"""
    if isinstance(model, str):
        return _string_targets_bedrock(model)
    provider = get_model_provider(model)
    if provider is not None and _fold_provider(provider) in _BEDROCK_PROVIDERS:
        return True
    return type(model).__name__ in _BEDROCK_CLASS_NAMES


def _qualified_spec_matches(model: BaseChatModel, spec: str, identifier: str | None) -> bool:
    if identifier is None:
        return False
    head, colon, tail = spec.partition(":")
    if not colon or tail != identifier:
        return False
    observed = get_model_provider(model)
    if observed is None:
        logger.debug(
            _PROVIDER_UNVERIFIED,
            spec,
            type(model).__module__,
            type(model).__name__,
            head,
        )
        return True
    return _fold_provider(head) == _fold_provider(observed)


def model_matches_spec(model: BaseChatModel, spec: str) -> bool:
    """整串相等优先于按冒号切开。切不开或模型半边不同就不再去问 provider。"""
    identifier = get_model_identifier(model)
    if identifier is not None and spec == identifier:
        return True
    return _qualified_spec_matches(model, spec, identifier)


def resolve_model(model: str | BaseChatModel) -> BaseChatModel:
    """实例原样返回。其余值把画像函数的返回值展开后交给 ``init_chat_model``。

    这条返回表达式先解析全局名，再调用画像函数。画像函数删掉或换掉这个全局时，调用的仍是解析到的那个函数。
    """
    if isinstance(model, BaseChatModel):
        return model
    return init_chat_model(model, **apply_provider_profile(model))
