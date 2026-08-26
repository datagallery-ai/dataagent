# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Job control plane (submit / poll / collect / cancel)."""

from dataagent.core.jobs.envelope import (
    INTERNAL_JOB_ENVELOPE_KEY,
    SUBMIT_JOB_TOOLS,
    build_base_job_envelope,
    envelope_from_tool_context,
    finalize_job_envelope,
)
from dataagent.core.jobs.models import ACTIVE_STATUSES, TERMINAL_STATUSES, JobResult, JobSnapshot
from dataagent.core.jobs.service import JobService

__all__ = [
    "ACTIVE_STATUSES",
    "INTERNAL_JOB_ENVELOPE_KEY",
    "JobResult",
    "JobService",
    "JobSnapshot",
    "SUBMIT_JOB_TOOLS",
    "TERMINAL_STATUSES",
    "build_base_job_envelope",
    "envelope_from_tool_context",
    "finalize_job_envelope",
]
