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
import subprocess
import sys


def test_cli_import_initializes_dataagent_logging_and_honors_env_level():
    """CLI imports should install DataAgent's Loguru sinks before commands run."""
    code = "\n".join(
        [
            "import importlib",
            "import os",
            "import sys",
            "os.environ['DATAAGENT_LOG_LEVEL'] = 'INFO'",
            "cli_main = importlib.import_module('dataagent.interface.cli.main')",
            "print('dataagent.utils.log' in sys.modules)",
            "cli_main.logger.debug('hidden debug from cli logger')",
            "cli_main.logger.info('visible info from cli logger')",
        ]
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "True"
    assert "hidden debug from cli logger" not in result.stderr
    assert "visible info from cli logger" in result.stderr
