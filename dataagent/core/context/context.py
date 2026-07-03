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
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from networkx.classes.digraph import DiGraph

from dataagent.core.context.context_ir import ActionNode, BaseIR, DataNode, StateNode
from dataagent.core.context.context_persistence import ContextPersistence
from dataagent.core.context.context_profiling import ContextProfiler
from dataagent.core.context.context_state import ContextState
from dataagent.core.context.todolist_manager import TodoListManager
from dataagent.core.context.trajectory_editor import TrajectoryEditor
from dataagent.core.context.trajectory_navigator import TrajectoryNavigator
from dataagent.utils.runtime_paths import resolve_layout_dir, resolve_session_framework_workspace


@dataclass(frozen=True, slots=True)
class ContextInitOptions:
    """Resolved Context settings passed into :class:`Context` (no ConfigManager reference)."""

    pre_workflow: tuple[dict[str, Any], ...] = ()
    post_workflow: tuple[dict[str, Any], ...] = ()
    workspace: str | Path | None = None
    config: Mapping[str, Any] | None = None


def build_context_init_options(
    config_manager: Any,
    *,
    workspace: str | Path | None = None,
) -> ContextInitOptions:
    """
    Build narrow Context init options from a per-Agent ConfigManager.

    Call from Runtime / FlexAgent boundaries only; Context does not read YAML config itself.

    Args:
        config_manager: Per-Agent :class:`~dataagent.config.config_manager.ConfigManager`.

    Returns:
        Frozen options for :meth:`ContextFactory.get_context`.
    """
    return ContextInitOptions(
        pre_workflow=tuple(config_manager.get("PRE_WORKFLOW", []) or []),
        post_workflow=tuple(config_manager.get("POST_WORKFLOW", []) or []),
        workspace=workspace,
        config=config_manager.get_all() if config_manager is not None else None,
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
                index_temp = (user_id, session_id, run_id, sub_id)
                while index_temp in cls._instances:
                    sub_id += 1
                    index_temp = (user_id, session_id, run_id, sub_id)

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
            cls._n_instances = 0


class Context:
    """
    Agent online trajectory manager (facade).

    Manages agent execution trajectory with the following structure:
    - `state`: ContextState instance
    - `nav`: TrajectoryNavigator instance
    - `persistence`: ContextPersistence instance
    - `editor`: TrajectoryEditor instance
    - `profiler`: ContextProfiler instance
    - `todolist_manager`: TodoListManager instance
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
        Initialize agent context manager.

        Args:
            user_id (str): user id
            session_id (str): current session id
            run_id (int): current run id within this session
            sub_id (int): current sub id of this run (0: main agent, >=1: sub agents)
            options: Resolved database URL and PRE/POST workflow definitions.
        """
        init_opts = options or ContextInitOptions()
        node_types: list[str] = [
            "Query",
            "Response",
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
        self.state = ContextState.build(
            user_id=user_id, session_id=session_id, run_id=run_id, sub_id=sub_id, node_types=node_types
        )
        if init_opts.workspace is not None:
            self.state.workspace = str(Path(init_opts.workspace).expanduser().resolve())
        self.state.config = init_opts.config
        self._nav = TrajectoryNavigator(ctx=self)
        self._persistence = ContextPersistence(ctx=self)
        self._editor = TrajectoryEditor(ctx=self)
        self._profiler = ContextProfiler(ctx=self)
        self._todolist_manager = TodoListManager(
            maxlen=100,
            pre_workflow=init_opts.pre_workflow,
            post_workflow=init_opts.post_workflow,
        )

    @property
    def initial_pt(self) -> str | None:
        """Initial query node id for this run, e.g. 'Query(query00000)'."""
        return self.state.initial_pt

    @property
    def session_root_pt(self) -> str | None:
        """Root Query node for the entire session, e.g. run 0's Query(query00000)."""
        return self.state.session_root_pt

    @property
    def has_initial_pt(self) -> bool:
        """Whether the initial query node for this run has been registered."""
        return self.state.initial_pt is not None

    @property
    def restored(self) -> bool:
        """Whether historical runs have been restored into this Context instance."""
        return self.state.restored

    @property
    def todolist_manager(self) -> TodoListManager:
        """The todolist manager of the context."""
        return self._todolist_manager

    @property
    def profiler(self) -> ContextProfiler:
        """The profiler of the context."""
        return self._profiler

    @property
    def editor(self) -> TrajectoryEditor:
        """The editor of the context."""
        return self._editor

    @property
    def persistence(self) -> ContextPersistence:
        """The persistence of the context."""
        return self._persistence

    @property
    def nav(self) -> TrajectoryNavigator:
        """The navigator of the context."""
        return self._nav

    @staticmethod
    def load_meta_from_json(
        *,
        user_id: str,
        session_id: str,
        run_id: int,
        sub_id: int = 0,
        workspace: str | Path | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Load metadata JSON for a given run (if exists).
        """
        framework_workspace = resolve_session_framework_workspace(
            workspace=workspace,
            config=config,
            session_id=session_id,
            user_id=user_id,
        )
        meta_path = (
            resolve_layout_dir(framework_workspace, "context_dir", config=config) / f"Run{run_id}_Sub{sub_id}.meta.json"
        )
        try:
            with open(meta_path) as f:
                meta = json.load(f)

            return meta if isinstance(meta, dict) else {}
        except Exception as e:
            logger.debug(f"Failed to load context meta from JSON file {meta_path}: {e}")
            return {}

    def profiling(self) -> None:
        """
        Perform data IR profiling for all data nodes in the trajectory. Note that this method is asynchronous and will
        not block the main thread, but it also means that the data IR profiling may not be completed before the main
        thread exits.
        """
        self._profiler.profiling()

    def update_state(self, *, graph_node_label: str) -> None:
        """
        Update state of a node when an additional execution direction is provided.

        Args:
            graph_node_label (str): graph node label, assumed to be in the form f'{node_type}({label})'
        """
        self._profiler.update_state(graph_node_label=graph_node_label)

    async def wait_pending_tasks(self) -> None:
        """Wait for async profiling / state-update tasks before persisting or closing."""
        await self._profiler.wait_pending_tasks()

    def restore_previous_runs(self, *, user_id: str, session_id: str, current_run_id: int, sub_id: int = 0) -> None:
        """
        Restore previous runs into the current context.

        Note: This method is idempotent per Context instance via `self._restored`.

        Args:
            user_id (str): user id
            session_id (str): session id
            current_run_id (int): current run id
            sub_id (int): sub-agent id
        """
        self._persistence.restore_previous_runs(
            user_id=user_id, session_id=session_id, current_run_id=current_run_id, sub_id=sub_id
        )

    def register_query(self, query: str, additional_files: list[str] | None = None) -> str:
        """
        Register a query for the current context.

        Args:
            query (str): query to register
            additional_files (list[str]): additional files to register

        Returns:
            Str, registered query nodes name in the form f"Query(query{sequence_number})"
        """
        return self._editor.register_query(query=query, additional_files=additional_files or [])

    def register_node(
        self,
        *,
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
        Register a node for the current context.

        Args:
            node_type (str): type of the node to register
            description (str): description of the node
            predecessor_node (list[str]): predecessor nodes of the node
            edge_type (str | None): edge type of the node
            label (str | None): label of the node
            add_pt (bool): whether to add a pointer to the node
            remove_pt (bool): whether to remove a pointer from the node
            **kwargs: additional keyword arguments to pass to the node constructor

        Returns:
            str: label of the registered node
        """
        return self._editor.register_node(
            node_type=node_type,
            description=description,
            predecessor_node=predecessor_node,
            edge_type=edge_type,
            label=label,
            add_pt=add_pt,
            remove_pt=remove_pt,
            **kwargs,
        )

    def remove_node(self, *, graph_node_label: str) -> None:
        """
        Remove a node from the current context.

        Args:
            graph_node_label (str): label of the graph node to remove
        """
        self._editor.remove_node(graph_node_label=graph_node_label)

    def modify_node(self, *, graph_node_label: str, changes: dict[str, Any]) -> None:
        """
        Modify a node in the current context. Modifications will be recorded in `history` of the node.

        Args:
            graph_node_label (str): label of the graph node to modify
            changes (dict[str, Any]): changes to apply to the node
        """
        self._editor.modify_node(graph_node_label=graph_node_label, changes=changes)

    def get_IR_from_node(self, *, graph_node_label: str) -> BaseIR:
        """
        Get the IR of a node from the current context.

        Args:
            graph_node_label (str): label of the graph node to get

        Returns:
            BaseIR: the IR instance of the node
        """
        re_match: re.Match | None = re.fullmatch(r"(.+)\((.+)\)", graph_node_label)
        if re_match is None:
            raise ValueError(f"Graph node label '{graph_node_label}' has illegal format.")

        node_type: str = re_match.group(1)
        label: str = re_match.group(2)
        IR: BaseIR = self.state.ir.get_IR(label=label, node_type=node_type)
        return IR

    def get_trajectory(self, *, trimmed: bool = False) -> DiGraph:
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
            return self._nav.trim_trajectory()

        return self.state.trajectory

    def get_all_historical_trajectories(self) -> dict[int, DiGraph]:
        """
        Get all historical trajectories of the current context.

        Returns:
            dict[int, DiGraph]: all historical trajectories of the current context
        """
        return self.state.historical_trajectories.copy()

    def get_active_branch(self) -> set[str]:
        """
        Get the active branch of the current context.

        Returns:
            set[str]: the active branch of the current context
        """
        return self.state.current_pt

    def get_next_data_node(self, *, action_node_label: str) -> list[DataNode]:
        """
        Get the next data node of the given action node.

        Args:
            action_node_label (str): label of the action node

        Returns:
            list[DataNode]: the next data node of the given action node
        """
        return self._nav.get_next_data_node(action_node_label=action_node_label)

    def persist_to_json(self) -> str:
        """
        Persist current run's context (nodes + edges) to JSON file.

        Only stores nodes and edges belonging to the current run_id.
        Historical nodes (merged into _trajectory for traversal) and
        cross-run bridging edges are not persisted — they are reconstructed
        by restore_previous_runs() + register_query() on the next load.

        Returns:
            str: path to the JSON file
        """
        return self._persistence.persist_to_json()

    def persist_meta_to_json(self) -> str:
        """
        Persist lightweight metadata for current run to a separate JSON file.

        内容仅包含：
        - initial_pt: 当前 run 的起始 Query 节点
        - current_pt: 当前 active branch 的端点集合
        - messages: Context.messages 中少量 JSON 友好的关键字段（如 pending_branch/_enriched_plan）

        Returns:
            str: path to the JSON file
        """
        return self._persistence.persist_meta_to_json()

    def show(self, *, output_html: str | None = None, current_run_only: bool = True) -> None:
        """
        Show the trajectory graph of the current context.

        Args:
            output_html (str | None): path to the output HTML file. Default to directory of other json files.
            current_run_only (bool): if True (default), visualize only the downstream subgraph from
                initial_pt (current run). If False, visualize the full merged trajectory.
        """
        try:
            from dataagent.core.context.context_viz import show_trajectory_graph
        except ImportError:
            logger.info(
                "Pyvis is required for trajectory visualization. "
                "Install it via `pip install dataagent[all]` or `pip install pyvis`."
            )
            return

        if output_html is None:
            workspace = resolve_session_framework_workspace(
                workspace=self.state.workspace,
                config=self.state.config,
                session_id=self.state.session_id,
                user_id=self.state.user_id,
            )
            output_path = (
                resolve_layout_dir(workspace, "context_dir", config=self.state.config)
                / f"Run{self.state.run_id}_Sub{self.state.sub_id}.html"
            )
        else:
            output_path = Path(output_html)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        trajectory = self._nav.subgraph_from_initial_pt() if current_run_only else self.state.trajectory
        show_trajectory_graph(trajectory=trajectory, output_html=str(output_path))

    def get_lineage(
        self, *, text_file_only: bool = False
    ) -> list[list[tuple[DataNode, ActionNode | None, StateNode | None]]]:
        """
        Get the lineage of the data IR.

        Args:
            text_file_only (bool): whether to only show text files

        Returns:
            list[list[tuple[DataNode, ActionNode | None, StateNode | None]]]: the lineage of the data IR
        """
        return self._nav.get_lineage(ir=self.state.ir, text_file_only=text_file_only)

    def append_todo(self, *, name: str, params: dict[str, Any] | None = None, list_type: str = "pre") -> bool:
        """
        Append a todo node to the specified todo list.

        Args:
            name (str): name of the todo node
            params (dict[str, Any], optional): parameters of the todo node. Defaults to None.
            list_type (str): type of the todo list, can be "pre" or "post". Defaults to "pre".

        Returns:
            bool: True if the node was added successfully, False otherwise.
        """
        if params is None:
            params = {}

        node: dict[str, Any] = {"name": name, "params": params}
        return self._todolist_manager.add_node(node=node, list_type=list_type)

    def pop_todo(self, *, list_type: str = "pre") -> dict[str, Any]:
        """
        Pop a todo node from the specified todo list.

        Args:
            list_type (str): type of the todo list, can be "pre" or "post".
            Defaults to "pre".

        Returns:
            dict[str, Any]: The popped todo node as a dictionary with
            'name' and 'params' keys, or None if the list is empty.
        """
        node = self._todolist_manager.pop_node(list_type=list_type)
        if node is None:
            return {}

        return {"name": node.name, "params": node.params}

    def get_full_data(self, *, graph_node_label: str) -> tuple[str, str]:
        """
        Get the full data of a node from the current context.

        Args:
            graph_node_label (str): label of the graph node to get

        Returns:
            Tuple[str, str]: path to the data, and the full data of the node
        """
        try:
            IR: BaseIR = self.get_IR_from_node(graph_node_label=graph_node_label)
            if isinstance(IR, DataNode):
                return getattr(IR, "path", ""), IR.get_full_data(from_backup=True)
            else:
                return "", "No full data available."
        except Exception:
            return "", "No full data available."

    def get_recorded_files(self) -> dict[str, tuple[str, str]]:
        """
        Return all recorded data object paths in this context.

        Returns:
            dict[str, tuple[str, str]]: {path: (graph_node_label, md5_hex)} for each data object's
                latest IR in the lineage
        """
        return self._nav.get_recorded_files()

    def add_edge_manually(self, *, from_node: str, to_node: str, edge_type: str) -> None:
        """
        Add an edge manually to the current context.

        Args:
            from_node (str): label of the from node
            to_node (str): label of the to node
            edge_type (str): type of the edge
        """
        self._editor.add_edge(from_node=from_node, to_node=to_node, edge_type=edge_type)
