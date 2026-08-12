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
import pytest

from dataagent.core.managers.llm_manager.llm_client import LLMCallError, LLMClient, LLMErrorCategory

_LONG = "a" * 32
_SK = "sk-" + "b" * 24
_BEARER = "Bearer " + "c" * 32


def _client() -> LLMClient:
    return LLMClient(model="demo", api_base="http://example.invalid/v1", api_key="k")


def _assert_blocked(content: str) -> None:
    with pytest.raises(LLMCallError) as exc:
        _client()._build_payload([{"role": "user", "content": content}], {}, stream=False)
    assert exc.value.category == LLMErrorCategory.CONTENT_POLICY


def _assert_allowed(content: str) -> None:
    payload = _client()._build_payload([{"role": "user", "content": content}], {}, stream=False)
    assert payload["messages"][0]["content"] == content


def test_build_payload_blocks_credential_like_content():
    _assert_blocked("api_key=sk-abcdefghijklmnopqrstuvwxyz")


def test_build_payload_allows_credential_prose_without_secret_value():
    """Natural-language mentions of password/api_key fields must not trip the gate."""
    for content in (
        "password: forgot",
        "pwd = unknown",
        "api_key: deprecated",
        "Please document the password column in the users table.",
    ):
        _assert_allowed(content)


def test_build_payload_allows_short_password_assignment():
    """Short key=value secrets are out of scope for the short-list gate."""
    _assert_allowed("password=Sup3rS3cret!xx")


def test_build_payload_blocks_long_token_assignment():
    _assert_blocked(f"api_key={_LONG}")


@pytest.mark.parametrize(
    "content",
    [
        # assign + CJK 前/后/前后
        f"密钥secret_key={_LONG}",
        f"secret_key={_LONG}的配置",
        f"密钥secret_key={_LONG}配置",
        # assign + fullwidth ：／＝
        f"密钥secret_key：{_LONG}",
        f"配置password＝{_LONG}",
        f"secret_key = {_LONG}",
        f'secret_key="{_LONG}"',
        '{"secret_key": "' + _LONG + '"}',
        # sk- / Bearer + CJK 前/后/前后；sk-proj 取前后代表
        f"密钥{_SK}",
        f"{_SK}的配置",
        f"密钥{_SK}配置",
        f"密钥sk-proj-{_LONG}配置",
        f"授权{_BEARER}",
        f"{_BEARER}的配置",
        f"密钥{_BEARER}配置",
        # Bearer 中文后缀：末字符为非 \\w / 为 \\w 都应命中
        "Bearer " + "c" * 31 + "-" + "无效",
        "Bearer " + "c" * 32 + "无效",
    ],
)
def test_build_payload_blocks_credential_boundary_variants(content: str):
    _assert_blocked(content)


@pytest.mark.parametrize(
    "content",
    [
        f"secret key={_LONG}",  # spaced key name
        f"x{_SK}",  # ASCII embed must not false-positive
        "bearer " + "c" * 32,  # Bearer is case-sensitive
    ],
)
def test_build_payload_allows_non_credential_boundary_shapes(content: str):
    _assert_allowed(content)
