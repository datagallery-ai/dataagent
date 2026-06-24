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
import json
from typing import Any

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.utils.nl2sql_utils import json_parser
from dataagent.agents.nl2sql.workflow.state import NL2SQLState
from dataagent.utils.log import logger


class CoordinatorNode(BaseNL2SQLNode):
    def __init__(self, **kwargs):
        super().__init__(name="coordinator", **kwargs)

    def _process(self, state: NL2SQLState, runtime: Any = None) -> NL2SQLState:
        self._trajectory_recorder.record_node_start(
            node_name="coordinator",
            purpose=f"Parse question into semantic question and keywords: {state['question']}",
        )
        context = {"question": state["question"]}
        res = json.loads(json_parser(self.execute_with_llm(context)))
        state["semantic_question"] = res["semantic_question"]
        state["keywords"] = res["keywords"]
        message = f"=== Coordinator ===\n{res}"
        logger.info(message)
        state["stream_message"] = message
        return state
