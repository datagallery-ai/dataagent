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
from typing import Any

import yaml

from dataagent.utils.env_utils import get_env


class LogConfig:
    """日志配置管理器"""

    def __init__(self):
        self.config_dir = Path(__file__).parent / "configs"
        self._config = None

    @staticmethod
    def get_env_config() -> dict[str, Any]:
        """从环境变量获取配置"""
        console_raw = get_env("DATAAGENT_LOG_CONSOLE", default="true")
        return {
            "level": get_env("DATAAGENT_LOG_LEVEL", default="INFO"),
            "file_path": get_env("DATAAGENT_LOG_FILE"),
            "console": (console_raw or "true").lower() == "true",
            "rotation": get_env("DATAAGENT_LOG_ROTATION", default="100 MB"),
            "retention": get_env("DATAAGENT_LOG_RETENTION", default="7 days"),
        }

    def load_config(self, config_name: str = "default") -> dict[str, Any]:
        """加载日志配置"""
        config_file = self.config_dir / f"{config_name}.yaml"

        if not config_file.exists():
            config_file = self.config_dir / "default.yaml"

        with open(config_file, encoding="utf-8") as f:
            return yaml.safe_load(f)


# 全局配置实例
log_config = LogConfig()
