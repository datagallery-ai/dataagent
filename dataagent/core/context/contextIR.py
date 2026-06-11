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
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any

import networkx as nx
import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
from loguru import logger

from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.core.utils.performance import attribute_calls


@dataclass
class BaseIR:
    """
    Dataclass model for Base node IR.
    """

    label: str  # 节点名称
    description: str | None  # 节点描述
    session_id: str  # 所属session id
    run_id: int  # 所属run id
    created_at: datetime = field(init=False)  # 创建时间
    history: dict[int, dict[str, Any]] = field(default_factory=dict, kw_only=True)  # IR历史记录

    def __post_init__(self):
        """
        Post-initialize base IR node. Auto fill-in timestamp and initialize history.
        """
        self.created_at = datetime.now(timezone(timedelta(hours=8)))

    @classmethod
    async def llm_infer_async(cls, system_prompt: str, user_prompt: str) -> str:
        """
        Uniform LLM inference.

        Args:
            system_prompt (str): system prompt
            user_prompt (str): user prompt

        Returns:
            str, inferred content
        """
        llm = llm_manager.get_default_llm()
        prompts = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            with attribute_calls("context", cls.__name__):
                response = await asyncio.to_thread(llm.invoke, prompts)
            return response.content
        except Exception:
            return "default description"

    def get_schema(self) -> dict[str, Any]:
        """
        Get label and description of IR node.

        Returns:
            Dict[str, Any], schema: label + description
        """
        return {
            "label": self.label,
            "description": self.description,
        }

    def get_full_content(self) -> dict[str, Any]:
        """
        Get full contents of IR Node.

        Returns:
            Dict[str, Any], returning all fields as full content
        """
        return asdict(self)


NODE_REGISTRY: dict[str, tuple[type[BaseIR], list[str]]] = {}


def register_node(node_type: str, required_fields: list[str]):
    """
    Decorator function for node registration.

    Args:
        node_type (str): node type
        required_fields (list[str]): required inputs during node initialization
    """

    def decorator(cls):
        NODE_REGISTRY[node_type] = (cls, required_fields)

        @wraps(cls)
        def wrapped_function(*args, **kwargs):
            return cls(*args, **kwargs)

        return wrapped_function

    return decorator


@register_node("Query", ["query", "additional_files"])
@dataclass
class QueryNode(BaseIR):
    """
    Dataclass model for Query node IR.
    """

    query: str  # 用户query
    additional_files: list[str]  # 辅助文件


@register_node("Action", ["action", "params", "output", "success"])
@dataclass
class ActionNode(BaseIR):
    """
    Dataclass model for Action node IR.
    """

    action: str  # 调用的工具名称
    params: dict[str, Any]  # 工具的入参
    output: Any  # 工具的返回值
    success: bool  # action是否执行成功


@register_node("State", ["state"])
@dataclass
class StateNode(BaseIR):
    """
    Dataclass model for State node IR.
    """

    state: str  # 当前agent的结论

    async def update_state_async(self, history: nx.DiGraph, new_action: dict[str, Any]) -> str:
        """
        Get new the state description of the node when an additional execution direction is provided.

        Args:
            history (nx.DiGraph): history search direction that have already been executed
            new_action (dict[str, Any]): new action to be executed

        Returns:
            str, updated state description
        """
        failed_trajectory = {
            "nodes": dict(history.nodes.data()),
            "edges": list(history.edges.data()),
        }
        prompt_variables = {
            "current_state": self.state,
            "failed_trajectory": failed_trajectory,
            "new_action_name": new_action.get("action", ""),
            "new_action_params": new_action.get("params", ""),
        }
        system_prompt = PromptTemplate.from_package_relative(
            f"{PROMPT_MD_PREFIX}/irmanager/system_update_state"
        ).content
        user_prompt = PromptTemplate.from_package_relative(
            f"{PROMPT_MD_PREFIX}/irmanager/user_update_state"
        ).apply_prompt_template(**prompt_variables)
        return await self.llm_infer_async(system_prompt, user_prompt)


@dataclass
class DataNode(BaseIR):
    """
    Abstract Data Node Base Class:
    - Provides default schema / full_content export logic
    """

    async def infer_description_async(self, **kwargs: Any) -> Any:
        """Infer description of a given node based on its attributes."""
        pass


@register_node("Knowledge", ["knowledge_type", "knowledge_content"])
@dataclass
class KnowledgeNode(DataNode):
    """
    Knowledge Node IR
    """

    knowledge_type: str  # e.g. "calculation", "domain"
    knowledge_content: str  # 知识内容（代码 / 文本）

    async def infer_description_async(self, **kwargs: Any) -> str:
        """
        Infer description of a knowledge node.

        Returns:
            str, inferred description
        """
        prompt_variables = {
            "node_type": self.__class__.__name__.replace("Node", ""),
            "label": self.label,
            "from_action": str(kwargs.get("from_action", {})),
            "from_state": str(kwargs.get("from_state", {})),
            "data_preview": self.knowledge_content[:600]
            if len(self.knowledge_content) > 600
            else self.knowledge_content,
            "extra_info": "No extra info provided.",
        }
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt, user_prompt)


@register_node("Tool", ["tool_params", "tool_returns"])
@dataclass
class ToolNode(DataNode):
    """
    Tool Node IR
    """

    tool_params: str
    tool_returns: str

    async def infer_description_async(self, **kwargs: Any) -> str:
        """
        Infer description of a tool node.

        Returns:
            str, inferred description
        """
        prompt_variables = {
            "node_type": self.__class__.__name__.replace("Node", ""),
            "label": self.label,
            "from_action": str(kwargs.get("from_action", {})),
            "from_state": str(kwargs.get("from_state", {})),
            "data_preview": str({"tool_params": self.tool_params, "tool_returns": self.tool_returns}),
            "extra_info": "No extra info provided.",
        }
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt, user_prompt)


@register_node("Table", ["path"])
@dataclass
class TableNode(DataNode):
    """
    Table Node IR
    """

    path: str  # 表路径

    @staticmethod
    def load_table(path: str, n_rows: int = -1) -> pd.DataFrame:
        """
        Load table from path.

        Args:
            path (str): path of the table, supported file types are .csv, .tsv and .parquet
            n_rows (int): number of rows to load, if -1, load all rows

        Returns:
            pd.DataFrame, loaded table of top rows
        """
        if path.endswith(".csv"):
            df = pd.read_csv(path, keep_default_na=False, nrows=n_rows if n_rows >= 0 else None)
            return df.loc[:, ~df.columns.str.match(r"^Unnamed:\s*\d+$")]

        if path.endswith(".tsv"):
            df = pd.read_csv(path, sep="\t", keep_default_na=False, nrows=n_rows if n_rows >= 0 else None)
            return df.loc[:, ~df.columns.str.match(r"^Unnamed:\s*\d+$")]

        if path.endswith(".parquet"):
            return pd.read_parquet(path)

        raise ValueError(f"Unsupported file type: {path}")

    async def infer_description_async(self, **kwargs: Any) -> str:
        """
        Infer description of a table node.

        Returns:
            str, inferred description
        """
        try:
            data_preview = self.load_table(path=self.path, n_rows=50).to_string(index=False)
        except Exception:
            data_preview = "No preview available."

        prompt_variables = {
            "node_type": self.__class__.__name__.replace("Node", ""),
            "label": self.label,
            "from_action": str(kwargs.get("from_action", {})),
            "from_state": str(kwargs.get("from_state", {})),
            "data_preview": data_preview,
            "extra_info": "No extra info provided.",
        }
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt, user_prompt)


@register_node("Column", ["from_table", "values", "supplementary_schemas"])
@dataclass
class ColumnNode(DataNode):
    """
    Column Node IR
    """

    from_table: str  # 来源表
    values: dict[str, Any] | None  # 列值
    supplementary_schemas: dict[str, Any]


@register_node("File", ["path", "source"])
@dataclass
class FileNode(DataNode):
    """
    File Node IR
    """

    path: str
    source: str

    @staticmethod
    def is_text_file(filepath: str, sample_size: int = 8192) -> bool:
        """
        Check if a file is a text file using heuristic method.

        Args:
            filepath (str): path of the file
            sample_size (int): size of the sample to read (Default: `8192`)

        Returns:
            bool, True if the file is a text file, False otherwise
        """
        with open(filepath, "rb") as f:
            sample = f.read(sample_size)
        if not sample:
            return True
        # binary files usually have null bytes
        if b"\x00" in sample:
            return False
        try:
            decoded = sample.decode("utf-8", errors="replace")
            # not utf-8 text if many characters are replaced
            replacement_count = decoded.count("\ufffd")
            return replacement_count <= max(1, len(decoded) * 0.01)  # at most 1% replacements
        except Exception:
            return False

    async def infer_description_async(self, **kwargs: Any) -> str:
        """
        Infer description of a file node.

        Returns:
            str, inferred description
        """
        if self.is_text_file(self.path):
            with open(self.path, encoding="utf-8") as f:
                lines = []
                for i, line in enumerate(f):
                    if i >= 50:
                        break
                    lines.append(line)
                data_preview = "".join(lines)
        else:
            data_preview = "No preview available."

        prompt_variables = {
            "node_type": self.__class__.__name__.replace("Node", ""),
            "label": self.label,
            "from_action": str(kwargs.get("from_action", {})),
            "from_state": str(kwargs.get("from_state", {})),
            "data_preview": data_preview,
            "extra_info": "No extra info provided.",
        }
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt, user_prompt)


@register_node("Script", ["script_content", "script_type", "path", "related_data_list"])
@dataclass
class ScriptNode(DataNode):
    """
    Script Node IR
    """

    script_content: str
    script_type: str
    path: str | None
    related_data_list: list[str]

    async def infer_description_async(self, **kwargs: Any) -> str:
        """
        Infer description of a script node.

        Returns:
            str, inferred description
        """
        prompt_variables = {
            "node_type": self.__class__.__name__.replace("Node", ""),
            "label": self.label,
            "from_action": str(kwargs.get("from_action", {})),
            "from_state": str(kwargs.get("from_state", {})),
            "data_preview": self.script_content[:600] if len(self.script_content) > 600 else self.script_content,
            "extra_info": f"This script is written in {self.script_type} language.",
        }
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/irmanager/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt, user_prompt)


@register_node("Skill", ["path"])
@dataclass
class SkillNode(DataNode):
    """
    Skill Node IR
    """

    path: str  # skill包路径


class IRManager:
    """
    Manager class of IR. Support IR recording and retrieval.
    """

    def __init__(self, node_types: list[str] | None = None) -> None:
        """
        Initialize IRManager.

        Args:
            node_types (Optional[list[str]]): node types to be managed
             (Default: `["Query", "State", "Action", "Knowledge", "Tool", "Table", "Column", "File",
              "Script", "Skill"]`)
        """
        if node_types is None:
            node_types = ["Query", "State", "Action", "Knowledge", "Tool", "Table", "Column", "File", "Script", "Skill"]

        self._nodes: dict[str, dict[str, BaseIR]] = {i: {} for i in node_types}

    @staticmethod
    def _format_IR(
        node_type: str, label: str, description: str | None, session_id: str, run_id: int, **kwargs: Any
    ) -> BaseIR:
        """
        Create IR based on given node_type and attributes.

        Args:
            node_type (str): node type, assumed to be an element in self._nodes
            label (str): node label
            description (Optional[str]): node description
            session_id (str): session id to which this node belongs
            run_id (int): run id to which this node belongs
            **kwargs: additional parameters to be passed to __init__() of various nodes: \
                - Query: ["query", "additional_files"] \
                - State: ["state"] \
                - Action: ["action", "params", "output", "success"] \
                - Knowledge: ["knowledge_type", "knowledge_content"] \
                - Tool: ["tool_params", "tool_returns"] \
                - Table: ["path"] \
                - Column: ["from_table", "values", "supplementary_schemas"] \
                - Skill: ["path"]

        Returns:
            BaseIR, created IR of given node_type
        """
        if node_type not in NODE_REGISTRY:
            raise ValueError(f"Unknown node type: '{node_type}'.")

        node_class, required_fields = NODE_REGISTRY[node_type]
        if any(i not in kwargs for i in required_fields):
            raise ValueError(f"A {node_type} node must contain info about '{required_fields}'.")

        init_fields = required_fields.copy()
        if "history" in kwargs:
            init_fields.append("history")

        kwargs = {i: kwargs[i] for i in init_fields}
        IR: BaseIR = node_class(label=label, description=description, session_id=session_id, run_id=run_id, **kwargs)
        return IR

    def iter_nodes(self) -> Generator[tuple[str, str, BaseIR]]:
        """Iterate over all stored IR nodes.

        Yields:
            tuple[str, str, BaseIR]: (node_type, label, node)
        """
        for node_type, node_dict in self._nodes.items():
            for label, node in node_dict.items():
                yield node_type, label, node

    def get_IR(self, label: str, node_type: str) -> BaseIR:
        """
        Retrieve IR based on node_type and label.

        Args:
            label (str): node label
            node_type (str): node type

        Returns:
            BaseIR, retrieved IR object
        """
        if node_type not in self._nodes:
            raise ValueError(f"Current context does not have IR of type '{node_type}'.")

        IR: BaseIR | None = self._nodes[node_type].get(label, None)
        if IR is None:
            raise ValueError(f"Cannot get IR with name '{label}' of type '{node_type}'.")

        return IR

    def add_IR(
        self,
        node_type: str,
        label: str,
        session_id: str,
        run_id: int,
        description: str | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Add IR to IRManager.

        Args:
            node_type (str): node type to be added
            label (str): node label to be added
            description (Optional[str]): node description to be added, if None will be inferred
            session_id (str): session id to which this node belongs
            run_id (int): run id to which this node belongs
            **kwargs: additional parameters to be passed to self._format_IR(): \
                - required: ["label", "description", "session_id", "run_id"] \
                - additional: follow comments in self._format_IR()
        """
        if node_type not in self._nodes:
            raise ValueError(f"Current context does not have IR of type '{node_type}'.")

        IR: BaseIR = self._format_IR(
            node_type=node_type,
            label=label,
            description=description,
            session_id=session_id,
            run_id=run_id,
            **kwargs,
        )
        if label not in self._nodes[node_type]:
            self._nodes[node_type][label] = IR
        else:
            raise ValueError("Multiple nodes with same label detected.")

    def modify_IR(self, label: str, node_type: str, changes: dict[str, Any]) -> None:
        """
        Modify current IR in IRManager.

        Args:
            label (str): node label to be modified
            node_type (str): node type to be modified
            changes (dict[str, Any]): changes to be applied, keys being node attributes and values being changes
        """
        if node_type not in self._nodes:
            raise ValueError(f"Current context does not have IR of type '{node_type}'.")

        if label not in self._nodes[node_type]:
            raise ValueError(f"Current context does not have IR with label '{label}' of type '{node_type}'.")

        version_id = len(self._nodes[node_type][label].history)
        self._nodes[node_type][label].history[version_id] = {}
        for attr, value in changes.items():
            if not isinstance(getattr(self._nodes[node_type][label], attr), type(value)):
                logger.warning(f"Variable type changes when assigning attribute '{attr}' for '{label}'.")

            self._nodes[node_type][label].history[version_id][attr] = getattr(self._nodes[node_type][label], attr)
            setattr(self._nodes[node_type][label], attr, value)

    def remove_IR(self, label: str, node_type: str) -> None:
        """
        Remove current IR in IRManager. Do nothing if IR does not exist.

        Args:
            label (str): node label to be removed
            node_type (str): node type to be removed
        """
        if node_type not in self._nodes:
            raise ValueError(f"Current context does not have IR of type '{node_type}'.")

        if label in self._nodes[node_type]:
            del self._nodes[node_type][label]
