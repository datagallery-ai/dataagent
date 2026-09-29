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
"""中间件包的公开入口。

文件系统、子代理、技能、记忆、摘要和评分标准的类型都在各自模块里。
目录比上游多了一层，导入路径跟着落点走，导出的仍是那些模块里的对象。
"""

from . import rubric as _rubric
from .summarization import (
    DEEPAGENTS_DEFAULT_SUMMARY_PROMPT,
    SummarizationMiddleware,
    SummarizationToolMiddleware,
    create_summarization_tool_middleware,
)
from .filesystem.filesystem import FilesystemMiddleware, FilesystemPermission
from .memory import MemoryMiddleware
from .skills import SkillsMiddleware
from .async_subagents import AsyncSubAgent, AsyncSubAgentMiddleware
from .subagents.subagents import CompiledSubAgent, SubAgent, SubAgentMiddleware

GRADER_SYSTEM_PROMPT = _rubric.GRADER_SYSTEM_PROMPT
RUBRIC_GRADER_MESSAGE_SOURCE = _rubric.RUBRIC_GRADER_MESSAGE_SOURCE
CriterionEval = _rubric.CriterionEval
CriterionFail = _rubric.CriterionFail
CriterionPass = _rubric.CriterionPass
GraderResponse = _rubric.GraderResponse
GraderVerdict = _rubric.GraderVerdict
RubricEvaluation = _rubric.RubricEvaluation
RubricMiddleware = _rubric.RubricMiddleware
RubricResult = _rubric.RubricResult
RubricState = _rubric.RubricState

__all__ = [
    "DEEPAGENTS_DEFAULT_SUMMARY_PROMPT", "GRADER_SYSTEM_PROMPT", "RUBRIC_GRADER_MESSAGE_SOURCE",
    "AsyncSubAgent", "AsyncSubAgentMiddleware", "CompiledSubAgent", "CriterionEval",
    "CriterionFail", "CriterionPass", "FilesystemMiddleware", "FilesystemPermission",
    "GraderResponse", "GraderVerdict", "MemoryMiddleware", "RubricEvaluation",
    "RubricMiddleware", "RubricResult", "RubricState", "SkillsMiddleware",
    "SubAgent", "SubAgentMiddleware", "SummarizationMiddleware", "SummarizationToolMiddleware",
    "create_summarization_tool_middleware",
]
