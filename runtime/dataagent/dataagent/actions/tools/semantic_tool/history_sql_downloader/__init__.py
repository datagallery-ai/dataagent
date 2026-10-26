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
"""History-SQL corpus downloader package.

- CLI (any cwd)::

    python -m dataagent.actions.tools.semantic_tool.history_sql_downloader \
        [--config PATH] [--output-dir PATH] [--compare-dir PATH] \
        [--page-size N] [--no-lock] [--if-enabled]

- In-process entry used by the evaluation bootstrap hook::

    from dataagent.actions.tools.semantic_tool.history_sql_downloader import (
        ensure_history_sql_corpus,
    )
    exit_code = ensure_history_sql_corpus(config_path)  # 0/1/2
"""

from dataagent.actions.tools.semantic_tool.history_sql_downloader.cli import main
from dataagent.actions.tools.semantic_tool.history_sql_downloader.downloader import (
    ensure_history_sql_corpus,
)

__all__ = ["ensure_history_sql_corpus", "main"]
