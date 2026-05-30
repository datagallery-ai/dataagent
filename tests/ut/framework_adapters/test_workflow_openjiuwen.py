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
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from dataagent.core.framework_adapters.runtime.workflow_openjiuwen import OpenJiuWenWorkflow


def test_merge_delta_consumes_remove_all_message() -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)
    old_message = HumanMessage(content="old")
    summary_message = HumanMessage(content="summary")
    latest_message = AIMessage(content="latest")

    merged = workflow._merge_delta(
        {"messages": [old_message]},
        {"messages": [RemoveMessage(id="__remove_all__"), summary_message, latest_message]},
    )

    assert merged["messages"] == [summary_message, latest_message]


def test_merge_delta_appends_regular_message_delta() -> None:
    workflow = OpenJiuWenWorkflow(nodes=[], router=None)
    old_message = HumanMessage(content="old")
    new_message = AIMessage(content="new")

    merged = workflow._merge_delta({"messages": [old_message]}, {"messages": [new_message]})

    assert merged["messages"] == [old_message, new_message]
