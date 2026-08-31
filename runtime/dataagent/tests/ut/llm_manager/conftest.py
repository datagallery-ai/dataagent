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
"""llm_manager 测试套件共享 fixture。"""

import os

import pytest

from dataagent.common_utils import outbound_tls

_OUTBOUND_ENV_KEYS = (
    outbound_tls.ENV_CA_FILE,
    outbound_tls.ENV_CLIENT_CERT,
    outbound_tls.ENV_CLIENT_KEY,
    outbound_tls.ENV_CIPHERS,
    outbound_tls.ENV_MODE,
    outbound_tls.ENV_SSL_SERVICES,
)


def _hard_clear_outbound_env() -> None:
    for name in _OUTBOUND_ENV_KEYS:
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def _isolate_outbound_tls():
    """确保 llm_manager 测试不受 outbound_tls 测试泄漏的 env 影响。

    ``tests/ut/common_utils/test_outbound_tls.py`` 中若干用例通过
    ``apply_certificate_config`` 直接写 ``os.environ``（非 monkeypatch）。
    这里用进程级 pop 清理，避免 ``monkeypatch.delenv`` 在 teardown 时把泄漏值还原回来。
    """
    _hard_clear_outbound_env()
    outbound_tls.reset_cache()
    yield
    _hard_clear_outbound_env()
    outbound_tls.reset_cache()
