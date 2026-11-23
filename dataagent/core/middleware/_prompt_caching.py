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
"""按厂商把提示缓存中间件追加到调用方给出的列表末尾。

三家都是同一种做法：真正要挂的时候才导入，导入成功才构造，构造完一家立刻追加。
Anthropic 不在模块导入期绑定，因此没装 langchain-anthropic 时本文件仍能导入。
挂上去的中间件自己会忽略非目标模型，这里不判断当前模型是谁。
"""

import logging
from importlib import import_module

logger = logging.getLogger(__name__)

_BEDROCK_MODULE = "langchain_aws.middleware.prompt_caching"
_FIREWORKS_MODULE = "langchain_fireworks.middleware.prompt_caching"


def _accepted_import_names(module_path: str) -> set[str]:
    """这个模块路径的每一段前缀。

    缺包时 ImportError.name 精确落在其中之一，才当作「这个厂商没装」。
    用 startswith 会把更长的无关模块名也吞掉。
    """
    pieces = module_path.split(".")
    return {".".join(pieces[:length]) for length in range(1, len(pieces) + 1)}


def _middleware_if_importable(module_path: str, class_name: str, unavailable_message: str):
    """导入成功就只用 ignore 策略构造；缺的是这个包本身则返回 None。"""
    try:
        loaded = import_module(module_path)
    except ImportError as error:
        if error.name not in _accepted_import_names(module_path):
            raise
        logger.debug(unavailable_message, exc_info=error)
        return None
    vendor_cls = getattr(loaded, class_name)
    return vendor_cls(unsupported_model_behavior="ignore")


def _create_bedrock_prompt_caching_middleware():
    """langchain-aws 能导入时，返回 Bedrock 的提示缓存中间件。"""
    return _middleware_if_importable(
        _BEDROCK_MODULE,
        "BedrockPromptCachingMiddleware",
        "Bedrock prompt caching middleware is unavailable.",
    )


def _create_fireworks_prompt_caching_middleware():
    """langchain-fireworks 能导入时，返回 Fireworks 的提示缓存中间件。"""
    return _middleware_if_importable(
        _FIREWORKS_MODULE,
        "FireworksPromptCachingMiddleware",
        "Fireworks prompt caching middleware is unavailable.",
    )


def _anthropic_middleware_if_importable():
    """langchain-anthropic 能导入时，返回 Anthropic 的提示缓存中间件。"""
    return _middleware_if_importable(
        "langchain_anthropic.middleware.prompt_caching",
        "AnthropicPromptCachingMiddleware",
        "Anthropic prompt caching middleware is unavailable.",
    )


def append_prompt_caching_middleware(middleware: list) -> None:
    """就地追加，返回 None。

    顺序固定为 Anthropic、Bedrock、Fireworks。每一家构造完，不是 None 就立刻追加，
    然后再构造下一家。后面的厂商抛错时，前面已经追加的实例留在列表里。
    导入失败得到的 None 不放进列表。连续调用会重复追加，不去重。
    """
    anthropic = _anthropic_middleware_if_importable()
    if anthropic is not None:
        middleware.append(anthropic)
    bedrock = _create_bedrock_prompt_caching_middleware()
    if bedrock is not None:
        middleware.append(bedrock)
    fireworks = _create_fireworks_prompt_caching_middleware()
    if fireworks is not None:
        middleware.append(fireworks)
