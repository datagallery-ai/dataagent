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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import networkx as nx
from networkx.classes.digraph import DiGraph

from dataagent.core.context.context_ir import IRManager


def _default_created_at() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


@dataclass
class ContextState:
    """
    Explicit bag of mutable fields shared by Context collaborators.

    TrajectoryEditor / TrajectoryNavigator / persistence / restore / profiler
    read and write these fields; they should not depend on Context's private attributes.
    """

    user_id: str
    session_id: str
    run_id: int
    sub_id: int
    node_counts: dict[str, int]
    ir: IRManager
    trajectory: DiGraph
    historical_trajectories: dict[int, DiGraph] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_default_created_at)
    initial_pt: str | None = None
    session_root_pt: str | None = None
    current_pt: set[str] = field(default_factory=set)
    restored: bool = False
    profiled_nodes: set[str] = field(default_factory=set)
    messages: dict[Any, Any] = field(default_factory=dict)
    pending_tasks: dict[str, list[asyncio.Task[Any]]] = field(default_factory=lambda: defaultdict(list))

    @staticmethod
    def build(
        *,
        user_id: str,
        session_id: str,
        run_id: int,
        sub_id: int,
        node_types: list[str],
    ) -> ContextState:
        """Build a ContextState instance."""
        return ContextState(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            sub_id=sub_id,
            node_counts=dict.fromkeys(node_types, 0),
            ir=IRManager(node_types=node_types),
            trajectory=nx.DiGraph(),
        )
