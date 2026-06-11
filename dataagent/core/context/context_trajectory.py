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
import asyncio
import json
import re
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import networkx as nx
from loguru import logger
from networkx.classes.digraph import DiGraph

from dataagent.core.context.contextIR import BaseIR, DataNode, IRManager, StateNode
from dataagent.core.context.todolist_manager import TodoListManager
from dataagent.utils.runtime_paths import resolve_session_root

_IR_ADD_SKIP_KEYS = frozenset(
    {"node_type", "label", "description", "session_id", "run_id", "user_id", "sub_id", "created_at", "history"}
)


@dataclass(frozen=True, slots=True)
class ContextInitOptions:
    """Resolved Context settings passed into :class:`Context` (no ConfigManager reference)."""

    database_url: str | None = None
    pre_workflow: tuple[dict[str, Any], ...] = ()
    post_workflow: tuple[dict[str, Any], ...] = ()


def build_context_init_options(config_manager: Any) -> ContextInitOptions:
    """
    Build narrow Context init options from a per-Agent ConfigManager.

    Call from Runtime / FlexAgent boundaries only; Context does not read YAML config itself.

    Args:
        config_manager: Per-Agent :class:`~dataagent.config.config_manager.ConfigManager`.

    Returns:
        Frozen options for :meth:`ContextFactory.get_context`.
    """
    return ContextInitOptions(
        database_url=config_manager.get("CONTEXT.database.url"),
        pre_workflow=tuple(config_manager.get("PRE_WORKFLOW", []) or []),
        post_workflow=tuple(config_manager.get("POST_WORKFLOW", []) or []),
    )


class ContextFactory:
    """
    Factory class to manage Context instances for different agent (sub-)runs.
    Each (sub-)run gets its own Context instance with isolated data.
    """

    _instances: dict[tuple[str, str, int, int], "Context"] = {}
    _n_instances: int = 0
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def get_context(
        cls,
        user_id: str,
        session_id: str,
        run_id: int,
        sub_id: int | None = None,
        options: ContextInitOptions | None = None,
    ) -> "Context":
        """
        Get or create a Context instance for a specific user.

        Args:
            user_id (str): user id
            session_id (str): session id
            run_id (str): run id of this session
            sub_id (Optional[int]): sub-agent id, 0: main agent; >=1: sub-agent (Default: `None`)
            options: Resolved Context settings from :func:`build_context_init_options`.

        Returns:
            Context, Context instance for the query.
        """
        with cls._lock:
            if sub_id is None and cls._n_instances == 0:
                raise ValueError("Cannot initialize Context class in main agent without sub_id.")

            if sub_id is None:
                sub_id = cls._n_instances

            index: tuple[str, str, int, int] = (user_id, session_id, run_id, sub_id)
            if index not in cls._instances:
                cls._instances[index] = Context(
                    user_id=user_id,
                    session_id=session_id,
                    run_id=run_id,
                    sub_id=sub_id,
                    options=options,
                )
                cls._n_instances += 1

            return cls._instances[index]

    @classmethod
    def clear_context(cls) -> None:
        """
        Clear all Context instances.
        """
        with cls._lock:
            cls._instances.clear()


class Context:
    """
    Agent online trajectory manager.

    Manages agent execution trajectory with the following structure:
    - `_trajectory`: Contains nodes and edges from ALL runs in the session
      (historical merged + current run), connected via cross-run bridging edges.
    - `_historical_trajectories`: Dictionary mapping run_id to historical trajectory graphs
      (kept for per-run inspection/debugging).
    - `_IR`: Contains all nodes (historical + current) for numbering continuity
    - `_session_root_pt`: The very first Query node (from run 0), root for DAG traversal.
    - `_initial_pt`: The Query node of the current run, marking where this run starts.
    """

    def __init__(
        self,
        user_id: str,
        session_id: str,
        run_id: int,
        sub_id: int,
        options: ContextInitOptions | None = None,
    ) -> None:
        """
        Initialize agent trajectory manager.

        Args:
            user_id (str): user id
            session_id (str): current session id
            run_id (int): current run id within this session
            sub_id (int): current sub id of this run (0: main agent, >=1: sub agents)
            options: Resolved database URL and PRE/POST workflow definitions.
        """
        node_types: list[str] = [
            "Query",
            "State",
            "Action",
            "Knowledge",
            "Tool",
            "Table",
            "Column",
            "File",
            "Script",
            "Skill",
        ]
        self._user_id: str = user_id
        self._session_id: str = session_id
        self._run_id: int = run_id
        self._sub_id: int = sub_id
        self._node_counts: dict[str, int] = dict.fromkeys(node_types, 0)
        self._IR: IRManager = IRManager(node_types=node_types)
        init_opts = options or ContextInitOptions()
        self._todolist_manager: TodoListManager = TodoListManager(
            maxlen=100,
            pre_workflow=init_opts.pre_workflow,
            post_workflow=init_opts.post_workflow,
        )
        self._trajectory: DiGraph = nx.DiGraph()
        self._historical_trajectories: dict[int, DiGraph] = {}
        self._created_at: datetime = datetime.now(timezone(timedelta(hours=8)))
        self._initial_pt: str | None = None
        self._session_root_pt: str | None = None
        self._current_pt: set[str] = set()
        self._restored: bool = False
        self._persisted: bool = False
        self._pg_url: str | None = init_opts.database_url
        self._pg_url = None
        self.messages: dict[Any, Any] = {}
        self.pending_tasks: dict[str, list[asyncio.Task[Any]]] = defaultdict(list)
        if self._pg_url:
            from dataagent.core.context.utils_context_storage import create_table

            create_table(url=self._pg_url)
        self._profiled_nodes: set[str] = set()

    @property
    def initial_pt(self) -> str | None:
        """Initial query node id for this run, e.g. 'Query(query00000)'."""
        return self._initial_pt

    @property
    def session_root_pt(self) -> str | None:
        """Root query node id for the entire session, e.g. 'Query(query00000)' from run 0."""
        return self._session_root_pt

    @property
    def has_initial_pt(self) -> bool:
        """Whether the initial query node for this run has been registered."""
        return self._initial_pt is not None

    @property
    def restored(self) -> bool:
        """Whether historical runs have been restored into this Context instance."""
        return self._restored

    @property
    def todolist_manager(self) -> TodoListManager:
        """The todolist of the context."""
        return self._todolist_manager

    @staticmethod
    def load_meta_from_json(user_id: str, session_id: str, run_id: int, sub_id: int = 0) -> dict[str, Any]:
        """
        Load metadata JSON for a given run (if exists).
        """
        meta_path = (
            resolve_session_root(user_id=user_id, session_id=session_id)
            / ".context"
            / f"Run{run_id}_Sub{sub_id}.meta.json"
        )
        try:
            with open(meta_path) as f:
                meta = json.load(f)
                return meta if isinstance(meta, dict) else {}
        except Exception as e:
            logger.debug(f"Failed to load context meta from JSON file {meta_path}: {e}")
            return {}

    @staticmethod
    def _infer_edge_type(edge_type: str | None, predecessor_node: list[str], node_type: str) -> str:
        """
        Infer edge type.

        Args:
            edge_type (Optional[str]): user provided edge type
            perdecessor_node (list[str]): list of predecessor nodes
            node_type (str): current node type

        Returns:
            Str, inferred edge type
        """
        if edge_type is not None:
            return edge_type

        if (
            all(i.startswith("Query") for i in predecessor_node) or all(i.startswith("State") for i in predecessor_node)
        ) and node_type == "Action":
            return "triggers"
        if (
            all(i.startswith("Query") for i in predecessor_node)
            or all(i.startswith("Action") for i in predecessor_node)
        ) and node_type == "State":
            return "has_conclusion"
        # 同一 run 内多轮对话：上一轮已是 State，本轮 Planner 再落 State 时前驱为 State
        if all(i.startswith("State") for i in predecessor_node) and node_type == "State":
            return "has_conclusion"

        raise ValueError("No default edge type detected. Please provide an edge type.")

    @staticmethod
    def _get_IR_from_json(*, user_id: str, session_id: str, run_id: int, sub_id: int) -> DiGraph:
        """
        Load IR from JSON file.
        """
        store_path = (
            resolve_session_root(user_id=user_id, session_id=session_id) / ".context" / f"Run{run_id}_Sub{sub_id}.json"
        )
        try:
            with open(store_path) as f:
                trajectory_dict = json.load(f)
                return nx.node_link_graph(data=trajectory_dict, edges="edges")
        except Exception as e:
            logger.warning(f"Failed to load context from JSON file: {e}")
            return DiGraph()

    def profiling(self) -> None:
        """
        Perform data IR profiling for all data nodes in the trajectory. Note that this method is asynchronous and will
        not block the main thread, but it also means that the data IR profiling may not be completed before the main
        thread exits.
        """
        for node, attrs in self._trajectory.nodes.data():
            if (
                attrs.get("node_type") not in ["Query", "State", "Action"]
                and not attrs.get("description")
                and node not in self._profiled_nodes
            ):
                self.pending_tasks["profiling"].append(asyncio.create_task(self.datair_profiling(node)))
                self._profiled_nodes.add(node)

    async def wait_pending_tasks(self) -> None:
        """
        等待 Context 内部异步任务（pending_tasks）完成。
        """
        for tasks in self.pending_tasks.values():
            if not tasks:
                continue
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.warning(f"Pending context task failed during stream finalization: {result}")

    async def datair_profiling(self, graph_node_label: str) -> None:
        """
        Perform data IR profiling for a single data node.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'
        """
        IR = cast(DataNode, self.get_IR_from_node(graph_node_label))
        try:
            from_action = self._get_previous_node_label(graph_node_label)[0]
            from_state = self._get_previous_node_label(from_action)[0]
            new_description = await IR.infer_description_async(
                from_action=asdict(self.get_IR_from_node(from_action)),
                from_state=asdict(self.get_IR_from_node(from_state)),
            )
        except Exception:
            # Data nodes along with query do not have two ancestors, so we only use from_state (a.k.a. from_query)
            from_state = self._get_previous_node_label(graph_node_label)[0]
            new_description = await IR.infer_description_async(
                from_action={},
                from_state=asdict(self.get_IR_from_node(from_state)),
            )
        self.modify_node(graph_node_label, {"description": new_description})

    def restore_previous_runs(self, *, user_id: str, session_id: str, current_run_id: int, sub_id: int = 0) -> None:
        """Restore all previous runs (run_id < current_run_id) for the same session into this Context.

        Note: This method is idempotent per Context instance via `self._restored`.
        """
        if self._restored:
            return
        if current_run_id <= 0:
            self._restored = True
            return

        for past_rid in range(current_run_id):
            try:
                history = self._get_IR_from_pg(user_id=user_id, session_id=session_id, run_id=past_rid, sub_id=sub_id)
                if not history:
                    self._restore_single_run_from_json(
                        user_id=user_id, session_id=session_id, past_rid=past_rid, sub_id=sub_id
                    )
                    continue
                self._restore_single_run_from_pg(history=history, past_rid=past_rid, session_id=session_id)
            except Exception as e:
                logger.warning(
                    f"Failed to load historical context from pg for user={user_id}, session={session_id}, "
                    f"run={past_rid}, sub={sub_id}: {e}"
                )
                continue

        self._bridge_consecutive_runs(current_run_id=current_run_id)
        self._set_session_root_from_history()
        self._restored = True

    def register_query(self, query: str, additional_files: list[str]) -> str:
        """
        Initialize trajectory graph by registering user query.

        Args:
            query (str): user query in this run
            addtional_files (list[str]): any additional files from user upload

        Returns:
            Str, registered query nodes name in the form f"Query(query{sequence_number})"
        """
        logger.debug(
            f"Context: Registering query for user={self._user_id}, session={self._session_id}, run={self._run_id}, "
            f"sub={self._sub_id}. Query='{query[:50]}...'"
        )
        if self._initial_pt is not None:
            raise RuntimeError("Agent cannot have more than one query in one run!")

        sequence_number: str = str(self._node_counts["Query"]).zfill(5)
        self._IR.add_IR(
            node_type="Query",
            label="query" + sequence_number,
            description="User query No." + sequence_number,
            session_id=self._session_id,
            run_id=self._run_id,
            query=query,
            additional_files=additional_files,
        )
        self._trajectory.add_node(
            node_for_adding=f"Query(query{sequence_number})",
            node_type="Query",
            description="User query No." + sequence_number,
            query=query,
            additional_files=additional_files,
            run_id=self._run_id,
        )
        self._node_counts["Query"] += 1
        self._initial_pt = f"Query(query{sequence_number})"
        self._current_pt.add(f"Query(query{sequence_number})")

        self._bridge_to_latest_history(query_node=f"Query(query{sequence_number})")

        # Set _session_root_pt for run_id == 0 (no history, current Query is the session root)
        if self._session_root_pt is None:
            self._session_root_pt = f"Query(query{sequence_number})"

        return f"Query(query{sequence_number})"

    def register_node(
        self,
        node_type: str,
        description: str,
        predecessor_node: list[str],
        edge_type: str | None = None,
        label: str | None = None,
        add_pt: bool = False,
        remove_pt: bool = False,
        **kwargs: Any,
    ) -> str:
        """
        Add node to IRManager and to current trajectory graph.

        Args:
            node_type (str): node type
            description (str): node description
            predecessor_node (list[str]): alias of its predecessor node
            edge_type (Optional[str]): edge type between incoming node and its predecessor node (Default: `None`)
            label (Optional[str]): IR label (Default: `f{node_type in lower case}{five digits}`)
            add_pt (bool): whether to add a new point or not; if True, a pointer will be added to the incoming node \
                (Default: `False`)
            remove_pt (bool): whether to remove a pointer or not; if True, a pointer to the predecessor node will be \
                removed (Default: `False`)
            **kwargs: additional parameters to be passed to self._IR.add_IR: \
                - additional: follow comments in IRManager.add_IR()

        Return:
            Str, registered node name in the form f"{node_type}({label})"
        """
        if not self._initial_pt:
            raise RuntimeError("Cannot register other node before registering query node.")

        self._validate_predecessor_nodes(predecessor_node)
        edge = self._infer_edge_type(edge_type, predecessor_node, node_type)

        if label is None:
            label = node_type.lower() + str(self._node_counts[node_type]).zfill(5)

        self._IR.add_IR(
            node_type=node_type,
            label=label,
            description=description,
            session_id=self._session_id,
            run_id=self._run_id,
            **kwargs,
        )
        node_name = f"{node_type}({label})"
        self._trajectory.add_node(
            node_for_adding=node_name,
            node_type=node_type,
            description=description,
            run_id=self._run_id,
            **kwargs,
        )
        for i in predecessor_node:
            self._trajectory.add_edge(u_of_edge=i, v_of_edge=node_name, edge_type=edge)

        self._node_counts[node_type] += 1
        self._update_current_pointer(node_type, predecessor_node, label, add_pt, remove_pt)
        return node_name

    async def update_state(self, graph_node_label: str, action: dict[str, Any], params: dict[str, Any]) -> None:
        """
        Update state of a node when an additional execution direction is provided.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'
            action (dict[str, Any]): name of tool to be executed
            params (dict[str, Any]): parameters of the action
        """
        previous_state = cast(StateNode, self.get_IR_from_node(graph_node_label))
        sub_nodes = nx.descendants(self._trajectory, graph_node_label)
        sub_nodes.add(graph_node_label)
        sub_history = cast(nx.DiGraph, self._trajectory.subgraph(sub_nodes).copy())
        new_state = await previous_state.update_state_async(
            history=sub_history,
            new_action={"action": action, "params": params},
        )
        self.modify_node(graph_node_label, {"state": new_state})

    def remove_node(self, graph_node_label: str) -> None:
        """
        Remove node in IRManager and in current trajectory graph.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'
        """
        re_match: re.Match | None = re.fullmatch(r"(.+)\((.+)\)", graph_node_label)
        if re_match is None:
            raise ValueError(f"Graph node label '{graph_node_label}' has illegal format.")

        if graph_node_label in self._current_pt:
            self._current_pt.remove(graph_node_label)
            for i in self._trajectory.predecessors(graph_node_label):
                self._current_pt.add(i)

        node_type: str = re_match.group(1)
        label: str = re_match.group(2)
        self._IR.remove_IR(label=label, node_type=node_type)
        self._trajectory.remove_node(graph_node_label)
        if not nx.is_weakly_connected(self._trajectory):
            logger.error(f"Trajectory becomes disconnected after removing graph node {graph_node_label}.")

    def modify_node(self, graph_node_label: str, changes: dict[str, Any]) -> None:
        """
        Modify node in IRManager and in current trajectory graph.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'
            changes (dict[str, Any]): changes to be applied, keys being node attributes and values being changes
        """
        logger.debug(f"Context: Modifying node={graph_node_label} with changes={changes}")
        re_match: re.Match | None = re.fullmatch(r"(.+)\((.+)\)", graph_node_label)
        if re_match is None:
            raise ValueError(f"Graph node label '{graph_node_label}' has illegal format.")

        node_type: str = re_match.group(1)
        label: str = re_match.group(2)
        self._IR.modify_IR(label=label, node_type=node_type, changes=changes)
        for attr, value in changes.items():
            self._trajectory.nodes[graph_node_label][attr] = value

    def get_IR_from_node(self, graph_node_label: str) -> BaseIR:
        """
        Get corresponding IR from graph node label.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'

        Returns:
            BaseIR, corresponding IR object from graph node label
        """
        re_match: re.Match | None = re.fullmatch(r"(.+)\((.+)\)", graph_node_label)
        if re_match is None:
            raise ValueError(f"Graph node label '{graph_node_label}' has illegal format.")

        node_type: str = re_match.group(1)
        label: str = re_match.group(2)
        IR: BaseIR = self._IR.get_IR(label=label, node_type=node_type)
        return IR

    def get_trajectory(self, trimmed: bool = False) -> DiGraph:
        """
        Return merged trajectory across all runs in this session.

        After restore_previous_runs(), historical nodes and edges are merged
        into _trajectory along with cross-run bridging edges, forming a single
        connected DAG from run 0's Query to the current run's active endpoints.

        Args:
            trimmed (bool): whether to return full trajectory or trimmed trajectory (Default: `False`)

        Returns:
            DiGraph, merged trajectory containing nodes from all runs in this session.
        """
        if trimmed:
            return self._trim_trajectory()
        else:
            return self._trajectory

    def get_historical_trajectory(self, run_id: int) -> DiGraph | None:
        """
        Get historical trajectory for a specific run_id.

        Args:
            run_id (int): The run_id to retrieve trajectory for

        Returns:
            DiGraph | None: Historical trajectory for the specified run_id, or None if not found.
        """
        return self._historical_trajectories.get(run_id)

    def get_all_historical_trajectories(self) -> dict[int, DiGraph]:
        """
        Get all historical trajectories.

        Returns:
            dict[int, DiGraph]: Dictionary mapping run_id to its trajectory graph.
        """
        return self._historical_trajectories.copy()

    def get_active_branch(self) -> set[str]:
        """
        Get endpoints for all active branches.

        Returns:
            Set[str], set of current pointers
        """
        return self._current_pt

    def get_previous_action_node(self, data_node_label: str) -> list[BaseIR]:
        """
        Get the previous action IR of a data node from its node label.

        Args:
            data_node_label (str): data node label, assumed to be in the form f'{node_type}({label})'

        Returns:
            list[BaseIR], list of previous action IRs
        """
        for computing_IR_prefix in ["Query", "State", "Action"]:
            if data_node_label.startswith(computing_IR_prefix):
                raise ValueError(f"Input '{data_node_label}' is not a data node label.")

        predecessors = self._get_previous_node_label(data_node_label)
        IRs = []
        for label in predecessors:
            if label.startswith("Action"):
                IRs.append(self.get_IR_from_node(label))
        return IRs

    def get_next_data_node(self, action_node_label: str) -> list[DataNode]:
        """
        Get the next data IR of an action node from its node label.

        Args:
            action_node_label (str): action node label, assumed to be in the form f'{node_type}({label})'
        """
        internal_node_prefix = ["Table"]
        data_node_prefix = ["Table", "Column", "Knowledge", "Tool", "Script", "File", "Skill"]

        if not action_node_label.startswith("Action"):
            raise ValueError(f"Input '{action_node_label}' is not an action node label.")
        # 如果不能取出node会报错，保证一定是有效值
        if self.get_IR_from_node(action_node_label) is None:
            raise ValueError(f"Input '{action_node_label}' is not a valid node label.")

        def _get_data_node_from_succesors(self, node_label: str) -> list[DataNode]:
            """Get the next data IR of a node"""
            successors = list(self._trajectory.successors(node_label))
            IRs = []
            for label in successors:
                for prefix in data_node_prefix:
                    if label.startswith(prefix):
                        IRs.append(self.get_IR_from_node(label))
                for prefix in internal_node_prefix:
                    if label.startswith(prefix):
                        IRs += _get_data_node_from_succesors(self, label)
            return IRs

        return _get_data_node_from_succesors(self, action_node_label)

    def persist_to_pg(self) -> None:
        """Persist current run's context (nodes + edges) to PostgreSQL.

        Notes:
        - **Idempotent per Context instance**: guarded by `self._persisted`.
        - **Scope**: only persists IR nodes whose `run_id == self._run_id`, and only
          persists edges where both endpoints belong to current run (by checking
          each node's `run_id` stored in the trajectory graph).
        - **Failure handling**: individual node/edge failures are logged and do not
          abort the whole persistence process.
        """
        logger.debug(
            f"Context: Persisting context to PostgreSQL for user={self._user_id}, session={self._session_id}, "
            f"run={self._run_id}, sub={self._sub_id}"
        )

        if getattr(self, "_persisted", False):
            return

        # 1) Persist nodes
        for node_type, label, node in self._IR.iter_nodes():
            # Only store nodes that belong to the current run
            if getattr(node, "run_id", None) != self._run_id:
                continue
            row = asdict(node)
            # Mandatory identifiers
            row.update(
                {
                    "user_id": self._user_id,
                    "sub_id": self._sub_id,
                }
            )
            try:
                self._save_IR_to_pg(node_type, row)
            except Exception as e:
                logger.warning(f"Failed to save {node_type}:{label} to pg: {e}")

        for source, target, attr in self._trajectory.edges(data=True):
            source_node = self._trajectory.nodes.get(source, {})
            target_node = self._trajectory.nodes.get(target, {})
            source_run_id = source_node.get("run_id")
            target_run_id = target_node.get("run_id")

            # Save edge only if both source and target belong to current run
            if source_run_id == self._run_id and target_run_id == self._run_id:
                row_edge = {
                    "user_id": self._user_id,
                    "sub_id": self._sub_id,
                    "session_id": self._session_id,
                    "run_id": self._run_id,
                    "source": source,
                    "target": target,
                    "relationship": attr.get("relationship") or attr.get("edge_type") or "related",
                }
                try:
                    self._save_IR_to_pg("IR_Edge", row_edge)
                except Exception as e:
                    logger.warning(f"Failed to save edge {source}->{target} to pg: {e}")

        self._persisted = True

    def persist_to_json(self) -> str:
        """
        Persist current run's context (nodes + edges) to JSON file.

        Only stores nodes and edges belonging to the current run_id.
        Historical nodes (merged into _trajectory for traversal) and
        cross-run bridging edges are not persisted — they are reconstructed
        by restore_previous_runs() + register_query() on the next load.
        """
        savepath = (
            resolve_session_root(user_id=self._user_id, session_id=self._session_id)
            / ".context"
            / f"Run{self._run_id}_Sub{self._sub_id}.json"
        )
        savepath.parent.mkdir(parents=True, exist_ok=True)

        # Filter to only current-run nodes and edges
        current_run_trajectory = nx.DiGraph()
        for node, attrs in self._trajectory.nodes.data():
            if attrs.get("run_id") == self._run_id:
                current_run_trajectory.add_node(node, **attrs)
        for source, target, attrs in self._trajectory.edges.data():
            source_run = self._trajectory.nodes.get(source, {}).get("run_id")
            target_run = self._trajectory.nodes.get(target, {}).get("run_id")
            if source_run == self._run_id and target_run == self._run_id:
                current_run_trajectory.add_edge(source, target, **attrs)

        with open(savepath, "w") as f:
            trajectory_dict = nx.node_link_data(current_run_trajectory, edges="edges")
            json.dump(trajectory_dict, f, indent=4, ensure_ascii=False, default=str)
        logger.debug(f"Persisted context to JSON file: {savepath}")
        return str(savepath)

    def persist_meta_to_json(self) -> str:
        """
        Persist lightweight metadata for current run to a separate JSON file.

        内容仅包含：
        - initial_pt: 当前 run 的起始 Query 节点
        - current_pt: 当前 active branch 的端点集合
        - messages: Context.messages 中少量 JSON 友好的关键字段（如 pending_branch/_enriched_plan）
        """
        savepath = (
            resolve_session_root(user_id=self._user_id, session_id=self._session_id)
            / ".context"
            / f"Run{self._run_id}_Sub{self._sub_id}.meta.json"
        )
        savepath.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {
            "initial_pt": self._initial_pt,
            "current_pt": list(self._current_pt),
            "messages": {},
        }
        # 仅快照少量 JSON 友好的 Context.messages 字段，避免序列化问题和体积过大
        msg_snapshot: dict[str, Any] = {}
        try:
            messages = self.messages
            for key in ("pending_branch", "_enriched_plan", "historical_messages"):
                if key in messages:
                    msg_snapshot[key] = messages[key]
        except Exception as e:
            logger.debug(f"Context: snapshot messages for meta failed: {e}")
        else:
            data["messages"] = msg_snapshot

        with open(savepath, "w") as f:
            json.dump(data, f, indent=4, ensure_ascii=False, default=str)
        logger.debug(f"Persisted context meta to JSON file: {savepath}")
        return str(savepath)

    def show(self, output_html: str) -> None:
        """
        Visualize trajectory graph using pyvis network.

        Args:
            output_html (str): full path of output html file
        """
        try:
            import pyvis
        except ImportError:
            logger.error(
                "Pyvis is required for trajectory visualization. "
                "Install it via `pip install dataagent[all]` or `pip install pyvis`."
            )
            return
        from dataagent.core.context.utils_context_trajectory import graph_to_html, html_config

        logger.trace(pyvis.__version__)
        config: dict[str, Any] = html_config(self._trajectory)
        graph_to_html(config=config, G=self._trajectory, output_html=output_html)

    def append_todo(self, name: str, params: dict[str, Any] | None = None, list_type: str = "todo") -> bool:
        """
        Append a todo node to the specified todo list.

        Args:
            name (str): name of the todo node
            params (dict[str, Any], optional): parameters of the todo node. Defaults to None.
            list_type (str): type of the todo list, can be "pre", "todo", or "post". Defaults to "todo".

        Returns:
            bool: True if the node was added successfully, False otherwise.
        """
        if params is None:
            params = {}
        node = {"name": name, "params": params}
        return self._todolist_manager.add_node(node, list_type=list_type)

    def pop_todo(self, list_type: str = "todo") -> dict[str, Any] | None:
        """
        Pop a todo node from the specified todo list.

        Args:
            list_type (str): type of the todo list, can be "pre", "todo", or "post".
            Defaults to "todo".

        Returns:
            dict[str, Any] | None: The popped todo node as a dictionary with
            'name' and 'params' keys, or None if the list is empty.
        """
        node = self._todolist_manager.pop_node(list_type=list_type)
        if node is None:
            return None
        return {"name": node.name, "params": node.params}

    def _get_IR_from_pg(self, user_id: str, session_id: str, run_id: int, sub_id: int) -> dict[str, list[dict]]:
        """
        Load IR from PostgreSQL database.

        Args:
            user_id (str): User ID
            session_id (str): Session ID
            run_id (int): Run Number
            sub_id (int): Sub Number

        Returns:
            list[dict]: Query results
        """
        if not self._pg_url:
            return {}
        from dataagent.core.context.utils_context_storage import get_IR_from_pg

        logger.debug(
            f"Context: Loading IR from PostgreSQL for user={user_id}, session={session_id}, run={run_id}, sub={sub_id}"
        )
        return get_IR_from_pg(url=self._pg_url, user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id)

    def _save_IR_to_pg(self, node_type: str, ir_data: dict[str, Any]) -> None:
        """
        Save the IR content to the PostgreSQL database.

        Args:
            node_type (str): Type of the node (e.g., "Query", "State", etc.)
            ir_data (dict[str, Any]): IR data to be saved
        """
        if not self._pg_url:
            return
        from dataagent.core.context.utils_context_storage import save_IR_to_pg

        save_IR_to_pg(url=self._pg_url, node_type=node_type, ir_data=ir_data)

    def _trim_trajectory(self) -> DiGraph:
        """
        Trim non-active branches in current trajectory.

        Returns:
            DiGraph, trimmed trajectory.
        """
        if not self._session_root_pt or not self._current_pt:
            return cast(DiGraph, self._trajectory.copy())

        # Preserve the original trimming semantics without enumerating all simple
        # paths: only actions that are ancestors of an active endpoint survive.
        # Once inactive actions are removed, keep the remaining subgraph that is
        # still reachable from the initial query node.
        active_actions: set[str] = set()
        stack = list(self._current_pt)
        visited: set[str] = set()

        while stack:
            node = stack.pop()
            if node in visited or node not in self._trajectory:
                continue
            visited.add(node)
            if str(node).startswith("Action"):
                active_actions.add(str(node))
            stack.extend(str(pred) for pred in self._trajectory.predecessors(node))

        all_actions = {str(node) for node in self._trajectory.nodes if str(node).startswith("Action")}
        inactive_actions = all_actions - active_actions

        graph = self._trajectory.copy()
        if inactive_actions:
            graph.remove_nodes_from(inactive_actions)

        reachable_nodes = nx.descendants(graph, self._session_root_pt)
        reachable_nodes.add(self._session_root_pt)
        return cast(DiGraph, graph.subgraph(reachable_nodes).copy())

    def _update_node_counts_from_label(self, node_type: str, label: str) -> None:
        """
        Update node_counts based on the sequence number extracted from label.
        Label format: {node_type.lower()}{sequence_number} (e.g., "query00000", "action00001")

        Args:
            node_type (str): node type
            label (str): node label
        """
        if node_type not in self._node_counts:
            return
        node_type_lower = node_type.lower()
        if label.startswith(node_type_lower):
            try:
                number_str = label.strip(node_type_lower)
                sequence_number = int(number_str)
                self._node_counts[node_type] = max(self._node_counts[node_type], sequence_number + 1)
            except (ValueError, IndexError):
                pass

    def _validate_predecessor_nodes(self, predecessor_node: list[str]) -> None:
        """
        Validate predecessor nodes.

        Args:
            predecessor_node (list[str]): list of predecessor nodes
        """
        if not predecessor_node:
            raise ValueError("At least one predecessor node is required.")

        for i in predecessor_node:
            if i not in list(self._trajectory):
                raise ValueError(f"Cannot find predecessor node {i} on the graph.")

    def _update_current_pointer(
        self,
        node_type: str,
        predecessor_node: list[str],
        label: str,
        add_pt: bool,
        remove_pt: bool,
    ) -> None:
        """
        Update current pointer by according to given input parameters.

        Args:
            node_type (str): current node type
            perdecessor_node (list[str]): list of predecessor nodes
            label (str): current node label
            add_pt (bool): whether to add a new point or not
            remove_pt (bool): whether to remove a pointer or not
        """
        new_node = f"{node_type}({label})"
        if add_pt:
            self._current_pt.add(new_node)
        elif remove_pt:
            for i in predecessor_node:
                self._current_pt.remove(i)
        elif node_type in ["State", "Action"]:
            for i in predecessor_node:
                self._current_pt.remove(i)
            self._current_pt.add(new_node)

    def _get_previous_node_label(self, node_label: str) -> list[str]:
        """
        Get the previous node labels of a node from its node label.

        Args:
            node_label (str): data node label, assumed to be in the form f'{node_type}({label})'

        Returns:
            list[str], list of previous node labels
        """
        internal_node_prefix = ["Table"]
        predecessors = list(self._trajectory.predecessors(node_label))
        for prefix in internal_node_prefix:
            if predecessors and predecessors[0].startswith(prefix):
                return self._get_previous_node_label(predecessors[0])

        return predecessors

    def _restore_single_run_from_json(self, *, user_id: str, session_id: str, past_rid: int, sub_id: int) -> None:
        json_trajectory = self._get_IR_from_json(user_id=user_id, session_id=session_id, run_id=past_rid, sub_id=sub_id)
        if json_trajectory.number_of_nodes() == 0:
            return
        self._historical_trajectories[past_rid] = json_trajectory
        for node, attrs in json_trajectory.nodes.data():
            node_type = attrs.get("node_type", "")
            label = attrs.get("label", attrs.get("description", ""))
            re_match = re.fullmatch(r"(.+)\((.+)\)", node)
            if re_match:
                node_type_from_name = re_match.group(1)
                label_from_name = re_match.group(2)
                if not node_type:
                    node_type = node_type_from_name
                if not label or label == attrs.get("description", ""):
                    label = label_from_name
            self._update_node_counts_from_label(node_type, label)
            try:
                self._IR.add_IR(
                    node_type=node_type,
                    label=label,
                    description=attrs.get("description", ""),
                    session_id=attrs.get("session_id", session_id),
                    run_id=attrs.get("run_id", past_rid),
                    **{k: v for k, v in attrs.items() if k not in _IR_ADD_SKIP_KEYS},
                )
            except ValueError as e:
                logger.debug(f"Context restore: skip duplicate IR node {node_type}:{label} ({e})")
            if node not in self._trajectory:
                self._trajectory.add_node(node, **attrs)
        for source, target, attrs in json_trajectory.edges.data():
            if (
                source in self._trajectory
                and target in self._trajectory
                and (source, target) not in self._trajectory.edges
            ):
                self._trajectory.add_edge(source, target, **attrs)

    def _restore_single_run_from_pg(self, *, history: dict[str, list[dict]], past_rid: int, session_id: str) -> None:
        historical_trajectory: DiGraph = nx.DiGraph()
        for node_type, rows in history.items():
            if node_type == "IR_Edge":
                continue
            for row in rows:
                label = row.get("label")
                description = row.get("description")
                if not label:
                    continue
                self._update_node_counts_from_label(node_type, label)
                kwargs = {k: v for k, v in row.items() if k not in _IR_ADD_SKIP_KEYS}
                try:
                    self._IR.add_IR(
                        node_type=node_type,
                        label=label,
                        description=description,
                        session_id=row.get("session_id", session_id),
                        run_id=row.get("run_id", past_rid),
                        **kwargs,
                    )
                except ValueError as e:
                    logger.debug(f"Context restore: skip duplicate IR node {node_type}:{label} ({e})")
                graph_node = f"{node_type}({label})"
                node_attrs = {k: v for k, v in row.items() if k != "history"}
                node_attrs["node_type"] = node_type
                if graph_node not in historical_trajectory:
                    historical_trajectory.add_node(graph_node, **node_attrs)
                if graph_node not in self._trajectory:
                    self._trajectory.add_node(graph_node, **node_attrs)
        for edge in history.get("IR_Edge", []):
            source = edge.get("source")
            target = edge.get("target")
            relationship = edge.get("relationship")
            if (
                source
                and target
                and source in historical_trajectory
                and target in historical_trajectory
                and (source, target) not in historical_trajectory.edges
            ):
                historical_trajectory.add_edge(source, target, relationship=relationship, edge_type=relationship)
            if (
                source in self._trajectory
                and target in self._trajectory
                and (source, target) not in self._trajectory.edges
            ):
                self._trajectory.add_edge(source, target, relationship=relationship, edge_type=relationship)
        if historical_trajectory.number_of_nodes() > 0:
            self._historical_trajectories[past_rid] = historical_trajectory

    def _bridge_consecutive_runs(self, *, current_run_id: int) -> None:
        for past_rid in range(current_run_id):
            if past_rid not in self._historical_trajectories:
                continue
            next_rid = past_rid + 1
            next_traj = self._historical_trajectories.get(next_rid)
            if next_traj is None:
                continue
            hist_traj = self._historical_trajectories[past_rid]
            past_leaves = [n for n in hist_traj.nodes if hist_traj.out_degree(n) == 0]
            next_query_nodes = [n for n in next_traj.nodes if n.startswith("Query") and next_traj.in_degree(n) == 0]
            if not next_query_nodes:
                continue
            next_query = next_query_nodes[0]
            for leaf in past_leaves:
                if (
                    leaf in self._trajectory
                    and next_query in self._trajectory
                    and (leaf, next_query) not in self._trajectory.edges
                ):
                    self._trajectory.add_edge(leaf, next_query, relationship="continues_to", edge_type="continues_to")

    def _set_session_root_from_history(self) -> None:
        run0_traj = self._historical_trajectories.get(0)
        if run0_traj:
            root_candidates = [n for n in run0_traj.nodes if n.startswith("Query") and run0_traj.in_degree(n) == 0]
            if root_candidates:
                self._session_root_pt = root_candidates[0]

    def _bridge_to_latest_history(self, query_node: str) -> None:
        if not self._restored or not self._historical_trajectories:
            return
        latest_hist_rid = max(self._historical_trajectories.keys())
        latest_hist = self._historical_trajectories.get(latest_hist_rid)
        if latest_hist:
            hist_leaves = [n for n in latest_hist.nodes if latest_hist.out_degree(n) == 0]
            for leaf in hist_leaves:
                if leaf in self._trajectory and (leaf, query_node) not in self._trajectory.edges:
                    self._trajectory.add_edge(leaf, query_node, relationship="continues_to", edge_type="continues_to")
