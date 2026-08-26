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
from langchain_core.messages import AIMessage, HumanMessage

from dataagent.core.flex.hooks.history_writer import load_messages, save_messages


def test_save_messages_under_custom_workspace(tmp_path) -> None:
    workspace = tmp_path / "custom-workspace"
    workspace.mkdir()
    messages = [HumanMessage(content="hi"), AIMessage(content="there")]

    save_messages("u1", "s1", messages, workspace=workspace)
    path = workspace / ".memory" / "messages.json"
    assert path.is_file()

    loaded = load_messages("u1", "s1", workspace=workspace)
    assert [msg.content for msg in loaded] == ["hi", "there"]
