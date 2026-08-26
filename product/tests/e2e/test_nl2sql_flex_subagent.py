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
from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path
from typing import Any

from loguru import logger  # type: ignore[reportMissingImports]

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))

from dataagent.interface.sdk.agent import DataAgent  # noqa: E402


async def main() -> bool:
    try:
        config_path = PROJECT_DIR / "dataagent" / "core" / "flex" / "examples" / "nl2sql_flex_e2e_subagent.yaml"
        agent = DataAgent.from_config(config_path)

        query = "帮我分析一下superhero男女性别的体重趋势，并生成一份图文报告"

        result: Any = await agent.chat(query, session_id=None)

        assert isinstance(result, dict), f"unexpected response type: {type(result)}"
        assert result.get("complete", False) is True, "flex workflow did not reach complete=True"

        messages = result.get("messages", []) or []
        contents: list[str] = []
        for m in messages:
            content = getattr(m, "content", None)
            if content is None:
                continue
            contents.append(str(content))

        joined = "\n".join(contents)

        # 子 agent 拉起失败时一般会返回类似：子 Agent 执行失败/Config YAML not found
        assert "子 Agent 执行失败" not in joined, "sub agent execution failed (error marker found)"
        assert "Config YAML not found" not in joined, "sub agent config path not found"

        logger.info("✅ Flex(subagent)->NL2SQL e2e completed.")
        return True
    except Exception as e:
        logger.error(f"❌ Workflow error: {e}\n{traceback.format_exc()}")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
