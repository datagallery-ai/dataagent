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
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, cast

from loguru import logger
from networkx.classes.digraph import DiGraph

from dataagent.core.context.context_ir import DataNode, StateNode

if TYPE_CHECKING:
    from dataagent.core.context.context import Context


class ContextProfiler:
    """Async data-IR profiling tasks."""

    def __init__(self, *, ctx: Context) -> None:
        self._ctx = ctx

    def profiling(self) -> None:
        """
        Automatically perform data-IR profiling for all data nodes in the trajectory. The pending task will be added
        to self._ctx.state.pending_tasks["profiling"]
        """
        for node, attrs in self._ctx.state.trajectory.nodes.data():
            if (
                attrs.get("node_type") not in ["Query", "Response", "State", "Action"]
                and not attrs.get("description")
                and node not in self._ctx.state.profiled_nodes
            ):
                self._ctx.state.pending_tasks["profiling"].append(
                    asyncio.create_task(self.datair_profiling(graph_node_label=node))
                )
                self._ctx.state.profiled_nodes.add(node)

    async def wait_pending_tasks(self) -> None:
        """
        Wait for all pending tasks to complete. This method is used to wait for all pending tasks to complete. It is
        used to ensure that all pending tasks are completed before the context is closed.
        """
        for tasks in self._ctx.state.pending_tasks.values():
            if not tasks:
                continue

            timeout = 300
            results = await asyncio.gather(
                *(asyncio.wait_for(t, timeout=timeout) for t in tasks),
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, asyncio.TimeoutError):
                    logger.warning(f"Pending context task timed out after {timeout}s: {result}")
                elif isinstance(result, asyncio.CancelledError):
                    logger.warning(f"Pending context task was cancelled: {result}")
                elif isinstance(result, Exception):
                    logger.warning(f"Pending context task failed during stream finalization: {result}")

            tasks.clear()

    async def datair_profiling(self, *, graph_node_label: str) -> None:
        """
        Perform data-IR profiling for a given data node.

        Args:
            graph_node_label (str): The label of the data node to be profiled.
        """
        IR = cast(DataNode, self._ctx.get_IR_from_node(graph_node_label=graph_node_label))
        nav = self._ctx.nav
        try:
            from_action = nav.get_previous_node_label(node_label=graph_node_label)[0]
            from_state = nav.get_previous_node_label(node_label=from_action)[0]
            new_description = await IR.infer_description_async(
                from_action=asdict(self._ctx.get_IR_from_node(graph_node_label=from_action)),
                from_state=asdict(self._ctx.get_IR_from_node(graph_node_label=from_state)),
            )
        except Exception:
            from_state = nav.get_previous_node_label(node_label=graph_node_label)[0]  # from_query actually
            new_description = await IR.infer_description_async(
                from_action={},
                from_state=asdict(self._ctx.get_IR_from_node(graph_node_label=from_state)),
            )

        self._ctx.editor.modify_node(graph_node_label=graph_node_label, changes={"description": new_description})

    async def update_state_from_failed_trajectory(
        self, *, graph_node_label: str, history: DiGraph, new_action: list[dict[str, Any]]
    ) -> None:
        """
        Update state of a node when an additional execution direction is provided.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'
        """
        state_node = cast(StateNode, self._ctx.get_IR_from_node(graph_node_label=graph_node_label))
        new_state = await state_node.update_state_async(history=history, new_action=new_action)
        try:
            new_state = json.loads(new_state)
            changes = {
                "goal": new_state.get("goal_intent", ""),
                "belief": new_state.get("belief_about_world", ""),
                "action_history": new_state.get("action_history_summary", ""),
                "current_status": new_state.get("current_position", ""),
                "available_actions": new_state.get("available_actions", ""),
                "feedback": new_state.get("user_feedback_state", ""),
                "uncentainty": new_state.get("epistemic_state", ""),
            }
            self._ctx.editor.modify_node(graph_node_label=graph_node_label, changes=changes)
        except Exception as e:
            logger.error(f"Failed to update state of {graph_node_label}: {e}")

    def update_state(self, *, graph_node_label: str) -> None:
        """
        Update state of a state node.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'
        """
        if not graph_node_label.startswith("State"):
            return

        failed_trajectory, new_action = self._ctx.nav.get_failed_trajectory_and_new_action(
            state_node_label=graph_node_label
        )
        self._ctx.state.pending_tasks["state_update"].append(
            asyncio.create_task(
                self.update_state_from_failed_trajectory(
                    graph_node_label=graph_node_label, history=failed_trajectory, new_action=new_action
                )
            )
        )
