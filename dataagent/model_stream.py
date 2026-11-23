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
"""Collect native model chunks without dropping tool calls, reasoning or usage."""

from langchain_core.messages import BaseMessageChunk, message_chunk_to_message


async def collect_model_stream(model, messages, **kwargs):
    """Return one complete native message; propagate errors without replaying.

    BaseChatModel.astream handles providers without streaming support itself.
    """
    complete = None
    async for chunk in model.astream(messages, **kwargs):
        complete = chunk if complete is None else complete + chunk
    if complete is None:
        raise RuntimeError("Model stream ended without a response")
    return (
        message_chunk_to_message(complete) if isinstance(complete, BaseMessageChunk) else complete
    )
