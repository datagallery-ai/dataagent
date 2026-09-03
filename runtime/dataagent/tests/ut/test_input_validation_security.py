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

from dataagent.utils import env_file_loader


@pytest.mark.parametrize(
    ("line", "expected_log"),
    [
        ("=super-secret", "Could not parse env file key"),
        ('API_KEY="super-secret', "Could not parse value for env key: API_KEY"),
    ],
)
def test_parse_failure_logs_context_without_secret(monkeypatch, line, expected_log):
    messages = []
    monkeypatch.setattr(
        env_file_loader.logger, "warning", lambda message, *args: messages.append(message.format(*args))
    )

    result = env_file_loader._parse_binding_line(line)

    assert result.success is False
    assert messages == [expected_log]
    assert "super-secret" not in messages[0]
