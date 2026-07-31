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


def _client() -> LLMClient:
    return LLMClient(model="demo", api_base="http://example.invalid/v1", api_key="k")


def test_build_payload_blocks_credential_like_content():
    client = _client()
    with pytest.raises(LLMCallError) as exc:
        client._build_payload(
            [{"role": "user", "content": "api_key=sk-abcdefghijklmnopqrstuvwxyz"}],
            {},
            stream=False,
        )
    assert exc.value.category == LLMErrorCategory.CONTENT_POLICY


def test_build_payload_allows_credential_prose_without_secret_value():
    """Natural-language mentions of password/api_key fields must not trip the gate."""
    client = _client()
    for content in (
        "password: forgot",
        "pwd = unknown",
        "api_key: deprecated",
        "Please document the password column in the users table.",
    ):
        payload = client._build_payload([{"role": "user", "content": content}], {}, stream=False)
        assert payload["messages"][0]["content"] == content


def test_build_payload_allows_short_password_assignment():
    """Short key=value secrets are out of scope for the short-list gate."""
    client = _client()
    content = "password=Sup3rS3cret!xx"
    payload = client._build_payload([{"role": "user", "content": content}], {}, stream=False)
    assert payload["messages"][0]["content"] == content


def test_build_payload_blocks_long_token_assignment():
    client = _client()
    token = "a" * 32
    with pytest.raises(LLMCallError) as exc:
        client._build_payload(
            [{"role": "user", "content": f"api_key={token}"}],
            {},
            stream=False,
        )
    assert exc.value.category == LLMErrorCategory.CONTENT_POLICY
