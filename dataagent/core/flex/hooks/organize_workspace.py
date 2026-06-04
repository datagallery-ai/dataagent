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
import re
import shutil

from loguru import logger

from dataagent.core.cbb.runtime import Runtime
from dataagent.core.flex.workflow.state import FlexState


def organize_workspace(state: FlexState, runtime: Runtime) -> FlexState:
    """定位 create_*.sql 和 insert_*.sql 文件位置，将它们放到 sql/ 目录下"""
    workspace_dir = runtime.workspace_dir
    if workspace_dir is None:
        return state

    workspace = workspace_dir.resolve()
    sql_dir = workspace / "sql"
    moved_files = []

    for file_path in workspace.iterdir():
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() != ".sql":
            continue
        if not re.match(r"^(create_|insert_)", file_path.name, re.IGNORECASE):
            continue

        try:
            sql_dir.mkdir(exist_ok=True)
            dest_path = sql_dir / file_path.name
            shutil.move(str(file_path), str(dest_path))
            moved_files.append(file_path.name)
            logger.info(f"Moved {file_path.name} to {sql_dir}")
        except Exception as e:
            logger.warning(f"Failed to move {file_path.name}: {e}")

    return state
