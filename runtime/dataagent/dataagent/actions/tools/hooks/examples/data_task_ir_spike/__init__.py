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
"""Independent DataTaskIR extraction and rendering spike."""

from dataagent.actions.tools.hooks.examples.data_task_ir_spike.fill import fill_field_template, fill_field_templates
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.render import render_context
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.template import load_template_catalog

__all__ = ["fill_field_template", "fill_field_templates", "load_template_catalog", "render_context"]
