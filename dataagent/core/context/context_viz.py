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
from typing import Any

from networkx.classes.digraph import DiGraph


def show_trajectory_graph(*, trajectory: DiGraph, output_html: str) -> None:
    """
    Show the trajectory graph of the current context.

    Args:
        trajectory (DiGraph): the trajectory graph to show
        output_html (str): the path to the output HTML file
    """
    from dataagent.core.context.utils_context_trajectory import graph_to_html, html_config

    config: dict[str, Any] = html_config(G=trajectory)
    graph_to_html(config=config, G=trajectory, output_html=output_html)
