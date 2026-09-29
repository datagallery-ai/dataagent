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
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""把一段文字接到系统消息后面。

已有内容块时，新块正文前面加两个换行；没有旧块时按传入的对象原样追加。
返回值总是一条新的 SystemMessage，调用方手里的那条消息保持原样。
"""

import langchain_core.messages as lc_messages


def append_to_system_message(system_message: lc_messages.SystemMessage | None, text: str) -> lc_messages.SystemMessage:
    """在系统消息末尾追加一块文本。

    第一个参数为假时不读 content_blocks。已有块时，新块文本前面是两个换行，
    这一步用 f-string，所以走对象的 format 而不是 str。
    """
    blocks = list(system_message.content_blocks) if system_message else []
    if blocks:
        text = f"\n\n{text}"
    blocks.append({"type": "text", "text": text})
    return lc_messages.SystemMessage(content_blocks=blocks)
