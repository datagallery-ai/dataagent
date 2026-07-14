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
from pathlib import Path

from dataagent.utils.log import dataagent_logger


def test_logger_sinks_disable_diagnose(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    monkeypatch.setattr(dataagent_logger._loguru_logger, "remove", lambda: None)
    monkeypatch.setattr(dataagent_logger._loguru_logger, "add", lambda *args, **kwargs: calls.append(kwargs) or 1)
    monkeypatch.setattr(dataagent_logger.DataAgentLogger, "_initialized", False)
    monkeypatch.setattr(dataagent_logger.DataAgentLogger, "_logger_instances", {})

    dataagent_logger.DataAgentLogger.init_logger(
        dataagent_logger.LoggerConfig(
            console=True,
            file_path=str(tmp_path / "dataagent.log"),
            file_path_explicit=True,
        )
    )

    assert calls
    assert all(call.get("diagnose") is False for call in calls)
