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
import contextlib
import io
import json
import os
import sys
from pathlib import Path

from loguru import logger
from mcp.server.fastmcp import FastMCP

PROJECT_DIR = PROJECT_DIR = Path(__file__).resolve().parents[4]
sys.path.append(PROJECT_DIR)
from dataagent.interface.sdk.agent import DataAgent  # noqa: E402

mcp = FastMCP()


@mcp.tool()
async def nl2sql(query: str) -> str:
    """Query the database using natural language.

    Args:
        query: Natural language query.

    Returns:
        str: JSON string with the following structure:
        {
            "sql": Generated SQL,
            "columns": List of result columns,
            "rows": List of result rows,
        }
    """
    deploy_zone = os.getenv("DEPLOY_ZONE", "")
    if deploy_zone == "internal":
        proxy = os.getenv("HTTP_PROXY")
        if not proxy:
            logger.warning("黄区未配置代理，将无法访问外部模型")
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy

    logger.remove()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        agent = DataAgent.from_config(PROJECT_DIR / "dataagent" / "agents" / "nl2sql" / "nl2sql_agent.yaml")
        state = await agent.chat(query)
    res = {"sql": state["sql"], "columns": state["columns"], "rows": state["rows"]}
    return json.dumps({"original_msg": res, "frontend_msg": res}, default=str)


if __name__ == "__main__":
    mcp.run()
