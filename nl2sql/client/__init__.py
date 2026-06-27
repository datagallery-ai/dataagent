class LLMMaxRetriesExceeded(Exception):
    """LLM网络调用达到最大重试次数时抛出"""
    pass


class LLMParseMaxRetriesExceeded(Exception):
    """LLM响应解析失败达到最大重试次数时抛出"""
    pass


from .llm_client import LlmClient
from .llm_adapter import LLMAdapter, LLMResponse

__all__ = ["LlmClient", "LLMAdapter", "LLMResponse",
           "LLMMaxRetriesExceeded", "LLMParseMaxRetriesExceeded"]
