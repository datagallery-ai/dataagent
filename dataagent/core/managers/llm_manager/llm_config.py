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
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "authorization",
        "bearer",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
    }
)
_SENSITIVE_SUFFIXES = ("_api_key", "_token", "_secret", "_password", "_key")
_URL_KEYS = frozenset({"base_url", "api_base", "url", "endpoint"})


def _mask_secret(value: Any) -> Any:
    """Mask a secret: blank → ``<empty>``, ≤4 → ``***``, else keep length and last 4."""
    if value is None:
        return None
    if not isinstance(value, str):
        return "***"
    if not value.strip():
        return "<empty>"
    if len(value) <= 4:
        return "***"
    return "*" * (len(value) - 4) + value[-4:]


def _normalize_key(key: Any) -> str:
    """Fold key spelling variants (``X-Api-Key``, ``api-key``) onto one form."""
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def _is_sensitive_key(key: Any) -> bool:
    """Return True when the value under ``key`` must never be exported verbatim."""
    normalized = _normalize_key(key)
    if normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES):
        return True
    return "authorization" in normalized


def _redact_url_userinfo(value: str) -> str:
    """Mask only the password in a URL; username, host, port, path and query stay visible."""
    parts = urlsplit(value)
    if not parts.username and not parts.password:
        return value
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = quote(parts.username or "", safe="")
    if parts.password is not None:
        userinfo = f"{userinfo}:{_mask_secret(parts.password)}"
    netloc = f"{userinfo}@{host}" if host else userinfo
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _redact_sequence(values: Any, *, sensitive: bool) -> list[Any]:
    """Redact list/tuple items so secrets nested in ``default_headers`` are covered too."""
    redacted: list[Any] = []
    for item in values:
        if isinstance(item, dict):
            redacted.append(_redact_mapping(item))
        elif isinstance(item, (list, tuple)):
            redacted.append(_redact_sequence(item, sensitive=sensitive))
        elif sensitive:
            redacted.append(_mask_secret(item))
        else:
            redacted.append(item)
    return redacted


def _redact_mapping(params: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of ``params`` for export; never used for client construction."""
    redacted: dict[str, Any] = {}
    for key, value in params.items():
        sensitive = _is_sensitive_key(key)
        if isinstance(value, dict):
            redacted[key] = _redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            redacted[key] = _redact_sequence(value, sensitive=sensitive)
        elif sensitive:
            redacted[key] = _mask_secret(value)
        elif _normalize_key(key) in _URL_KEYS and isinstance(value, str):
            redacted[key] = _redact_url_userinfo(value)
        else:
            redacted[key] = value
    return redacted


class LLMConfig:
    """维护管理LLM的配置"""

    def __init__(
        self,
        name: str,
        provider: str,
        model_type: Literal["chat", "embedding"],
        section: str | None = None,
        **client_kwargs,
    ):
        """
        初始化LLM配置

        Args:
            name: config唯一标识名称，用于注册、缓存llm实例
            provider: 提供商名称
            model_type: 模型类型，支持"chat"或"embedding"
            section: 对应 YAML 中 MODEL 下的 key，由 LLMManager 在 init_from_config 时通过 section 字段注入。
            **client_kwargs: 实例化LLM实例时，传入给官方API的参数（如model、api_key、base_url等）
        """
        self.name = name
        self.provider = provider
        self.model_type = model_type
        if self.model_type not in ["chat", "embedding"]:
            raise ValueError(f"不支持的模型类型: {type}，支持的类型为: chat, embedding")
        self.section = section or name

        # 所有其他参数都存储在 extra_params 字典中
        self.client_kwargs = client_kwargs.copy()

    def __repr__(self) -> str:
        return (
            f"LLMConfig(name={self.name!r}, provider={self.provider!r}, "
            f"model_type={self.model_type!r}, section={self.section!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    @classmethod
    def from_dict(cls, config_dict: dict) -> "LLMConfig":
        """从字典创建LLMConfig实例"""
        config = config_dict.copy()
        if not all(k in config for k in ["name", "provider", "model_type"]):
            raise ValueError("name, provider, model_type参数是必需的")
        return cls(**config)

    def to_dict(self, *, redact: bool = True) -> dict:
        """导出配置；默认脱敏。内部建连请用 ``client_params()`` 或 ``redact=False``。

        ``redact=False`` 仅用于可信进程内重建，不要把结果交给日志或 REST。
        """
        result = {
            "name": self.name,
            "provider": self.provider,
            "model_type": self.model_type,
        }
        params = dict(self.client_params() or {})
        result.update(_redact_mapping(params) if redact else params)
        return result

    def client_params(self) -> dict:
        """获取实例化LLM的参数"""

        return self.client_kwargs["params"]

    def create_llm(self):
        """创建LLM实例 - 兼容旧接口"""
        from dataagent.core.managers.llm_manager.llm_manager import LLMManager

        return LLMManager().create_llm(self)
