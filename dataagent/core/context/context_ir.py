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
import re
import shutil
from collections import defaultdict
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import networkx as nx
import yaml
from loguru import logger

from dataagent.core.context.utils_context_filesystem import is_text_file, lineage_path_key, load_file, load_table
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from dataagent.core.utils.performance import attribute_calls
from dataagent.utils.runtime_paths import resolve_session_root


@dataclass
class BaseIR:
    """
    Dataclass model for Base node IR.
    """

    label: str  # 节点名称
    description: str | None  # 节点描述
    user_id: str  # 所属用户 id
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
    async def llm_infer_async(cls, *, system_prompt: str, user_prompt: str) -> str:
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


@dataclass
class QueryNode(BaseIR):
    """
    Dataclass model for Query node IR.
    """

    query: str  # 用户query，可被上下文指代改写
    additional_files: list[str]  # 辅助文件
    raw_user_query: str = ""  # 用户原始query，注册时等于原始输入，改写时不更新


@dataclass
class ResponseNode(BaseIR):
    """
    Dataclass model for Response node IR.
    """

    response: str  # agent回答
    reasoning_content: str  # agent的原始推理思考轨迹


@dataclass
class ActionNode(BaseIR):
    """
    Dataclass model for Action node IR.
    """

    action: str  # 调用的工具名称
    params: dict[str, Any]  # 工具的入参
    output: Any  # 工具的返回值
    success: bool  # action是否执行成功


@dataclass
class StateNode(BaseIR):
    """
    Dataclass model for State node IR.
    """

    content: str  # agent的原始推理输出内容
    reasoning_content: str  # agent的原始推理思考轨迹
    goal: str  # 当前agent的目标
    belief: str  # 当前agent的信念
    action_history: str  # 当前agent的行动历史摘要
    current_status: str  # 当前agent的位置
    available_actions: str  # 当前agent的可选动作
    feedback: str  # 当前agent得到的人工反馈
    uncentainty: str  # 当前agent的不确定性

    async def update_state_async(self, *, history: nx.DiGraph, new_action: list[dict[str, Any]]) -> str:
        """
        Get new the state description of the node when additional execution directions are provided.

        Args:
            history (nx.DiGraph): history search direction that have already been executed
            new_action (list[dict[str, Any]]): new actions to be executed

        Returns:
            str, updated state description
        """
        failed_trajectory = {
            "nodes": dict(history.nodes.data()),
            "edges": list(history.edges.data()),
        }
        prompt_variables = {
            "current_state": {
                "goal_intent": self.goal,
                "belief_about_world": self.belief,
                "action_history_summary": self.action_history,
                "current_position": self.current_status,
                "available_actions": self.available_actions,
                "user_feedback_state": self.feedback,
                "epistemic_state": self.uncentainty,
            },
            "failed_trajectory": failed_trajectory,
            "new_actions": new_action,
        }
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/system_update_state").content
        user_prompt = PromptTemplate.from_package_relative(
            f"{PROMPT_MD_PREFIX}/context/user_update_state"
        ).apply_prompt_template(**prompt_variables)
        return await self.llm_infer_async(system_prompt=system_prompt, user_prompt=user_prompt)


@dataclass
class DataNode(BaseIR):
    """
    Abstract Data Node Base Class:
    - Provides default schema / full_content export logic
    """

    def __post_init__(self):
        """
        Post-initialize data node. Auto backup file if the node has a path.
        """
        super().__post_init__()
        path = getattr(self, "path", None)
        if hasattr(self, "path_backup") and isinstance(path, str) and path and not self.path_backup:
            p = Path(path).expanduser()
            if p.exists() and p.is_file():
                node_type = self.__class__.__name__.replace("Node", "")
                backup_path = (
                    resolve_session_root(user_id=self.user_id, session_id=self.session_id)
                    / ".context"
                    / "backup"
                    / f"{node_type}({self.label}){p.suffix}"
                )
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src=p, dst=backup_path)
                self.path_backup = str(backup_path)

    async def infer_description_async(self, **kwargs: Any) -> Any:
        """Infer description of a given node based on its attributes."""
        pass

    def get_full_data(self, *, from_backup: bool = False) -> str:
        """
        Get full data of the node.
        """
        raise NotImplementedError("Subclasses must implement this method.")


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
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt=system_prompt, user_prompt=user_prompt)

    def get_full_data(self, *, from_backup: bool = False) -> str:
        return self.knowledge_content


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
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt=system_prompt, user_prompt=user_prompt)

    def get_full_data(self, *, from_backup: bool = False) -> str:
        return str({"tool_params": self.tool_params, "tool_returns": self.tool_returns})


@dataclass
class TableNode(DataNode):
    """
    Table Node IR
    """

    path: str  # 表路径
    path_backup: str = field(default="")  # 表备份路径

    async def infer_description_async(self, **kwargs: Any) -> str:
        """
        Infer description of a table node.

        Returns:
            str, inferred description
        """
        try:
            data_preview = load_table(path=self.path, n_rows=50).to_string(index=False)
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
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt=system_prompt, user_prompt=user_prompt)

    def get_full_data(self, *, from_backup: bool = False) -> str:
        try:
            if from_backup and self.path_backup:
                return load_table(path=self.path_backup).to_string(index=False)
            return load_table(path=self.path).to_string(index=False)
        except Exception:
            return f"Unsupported file type: {self.path}, please use SQL or other tools to load the data if necessary."


@dataclass
class ColumnNode(DataNode):
    """
    Column Node IR
    """

    from_table: str  # 来源表
    values: dict[str, Any] | None  # 列值
    supplementary_schemas: dict[str, Any]

    def get_full_data(self, *, from_backup: bool = False) -> str:
        """
        Get full data of the column node.
        """
        return f"Please read data from the table directly: {self.from_table}"


@dataclass
class FileNode(DataNode):
    """
    File Node IR
    """

    path: str
    source: str
    path_backup: str = field(default="")

    async def infer_description_async(self, **kwargs: Any) -> str:
        """
        Infer description of a file node.

        Returns:
            str, inferred description
        """
        try:
            data_preview = load_file(filepath=self.path, max_lines=50)
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
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt=system_prompt, user_prompt=user_prompt)

    def get_full_data(self, *, from_backup: bool = False) -> str:
        try:
            if from_backup and self.path_backup:
                return load_file(filepath=self.path_backup)
            return load_file(filepath=self.path)
        except Exception:
            return f"Binary file detected: {self.path}. Cannot read the content."


@dataclass
class ScriptNode(DataNode):
    """
    Script Node IR
    """

    script_content: str
    script_type: str
    related_data_list: list[str]
    path: str = field(default="")
    path_backup: str = field(default="")

    async def infer_description_async(self, **kwargs: Any) -> str:
        """
        Infer description of a script node.

        Returns:
            str, inferred description
        """
        data_preview = self.get_full_data()
        prompt_variables = {
            "node_type": self.__class__.__name__.replace("Node", ""),
            "label": self.label,
            "from_action": str(kwargs.get("from_action", {})),
            "from_state": str(kwargs.get("from_state", {})),
            "data_preview": data_preview[:600] if len(data_preview) > 600 else data_preview,
            "extra_info": f"This script is written in {self.script_type} language.",
        }
        system_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/system").content
        user_prompt = PromptTemplate.from_package_relative(f"{PROMPT_MD_PREFIX}/context/user").apply_prompt_template(
            **prompt_variables
        )
        return await self.llm_infer_async(system_prompt=system_prompt, user_prompt=user_prompt)

    def get_full_data(self, *, from_backup: bool = False) -> str:
        if from_backup and self.path_backup:
            return load_file(filepath=self.path_backup)
        if self.path:
            return load_file(filepath=self.path)
        if self.script_content:
            return self.script_content
        return "No data preview available."


@dataclass
class SkillNode(DataNode):
    """
    Skill Node IR
    """

    path: str

    async def infer_description_async(self, **kwargs: Any) -> str:
        try:
            content = load_file(filepath=self.path)
        except Exception:
            return "No description available."
        # 匹配形如下方的字符串，注意---最后需要以换行符作为结尾
        # ---
        # group 1
        # ---
        # group 2
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, flags=re.DOTALL)
        if match is None:
            return content[:600]
        try:
            meta = yaml.safe_load(match.group(1))
            desc = (meta or {}).get("description", "")
            if desc:
                return str(desc)
        except Exception:
            logger.warning("Cannot parse yaml formatter.")
        return content[:600]

    def get_full_data(self, *, from_backup: bool = False) -> str:
        try:
            content = load_file(filepath=self.path)
        except Exception:
            return f"Unable to read skill file: {self.path}"
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, flags=re.DOTALL)
        if match:
            return match.group(2)
        return content


NODE_REGISTRY: dict[str, type[BaseIR]] = {
    "Query": QueryNode,
    "Response": ResponseNode,
    "State": StateNode,
    "Action": ActionNode,
    "Knowledge": KnowledgeNode,
    "Tool": ToolNode,
    "Table": TableNode,
    "Column": ColumnNode,
    "File": FileNode,
    "Script": ScriptNode,
    "Skill": SkillNode,
}


class IRManager:
    """
    Manager class of IR. Support IR recording and retrieval.
    """

    def __init__(self, *, node_types: list[str] | None = None) -> None:
        """
        Initialize IRManager.

        Args:
            node_types (Optional[list[str]]): node types to be managed
             (Default: `["Query", "Response","State", "Action", "Knowledge", "Tool", "Table", "Column", "File",
              "Script", "Skill"]`)
        """
        if node_types is None:
            node_types = [
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

        self._nodes: dict[str, dict[str, BaseIR]] = {i: {} for i in node_types}

    @staticmethod
    def _format_IR(
        *,
        node_type: str,
        label: str,
        description: str | None,
        user_id: str,
        session_id: str,
        run_id: int,
        **kwargs: Any,
    ) -> BaseIR:
        """
        Create IR based on given node_type and attributes.

        Args:
            node_type (str): node type, assumed to be an element in self._nodes
            label (str): node label
            description (Optional[str]): node description
            user_id (str): user id to which this node belongs
            session_id (str): session id to which this node belongs
            run_id (int): run id to which this node belongs
            **kwargs: additional parameters to be passed to __init__() of various nodes:
                - Query: ["query", "additional_files", "raw_user_query"]
                - State: ["goal", "belief", "action_history", "current_status", "available_actions", "feedback",
                    "uncentainty", "content", "reasoning_content"]
                - Action: ["action", "params", "output", "success"]
                - Knowledge: ["knowledge_type", "knowledge_content"]
                - Tool: ["tool_params", "tool_returns"]
                - Table: ["path"]
                - Column: ["from_table", "values", "supplementary_schemas"]
                - File: ["path", "source"]
                - Script: ["script_content", "script_type", "related_data_list"]

        Returns:
            BaseIR, created IR of given node_type
        """
        if node_type not in NODE_REGISTRY:
            raise ValueError(f"Unknown node type: '{node_type}'.")

        node_class = NODE_REGISTRY[node_type]
        IR: BaseIR = node_class(
            label=label, description=description, user_id=user_id, session_id=session_id, run_id=run_id, **kwargs
        )
        return IR

    def iter_nodes(self) -> Generator[tuple[str, str, BaseIR]]:
        """Iterate over all stored IR nodes.

        Yields:
            tuple[str, str, BaseIR]: (node_type, label, node)
        """
        for node_type, node_dict in self._nodes.items():
            for label, node in node_dict.items():
                yield node_type, label, node

    def get_IR(self, *, label: str, node_type: str) -> BaseIR:
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
        *,
        node_type: str,
        label: str,
        user_id: str,
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
            user_id (str): user id to which this node belongs
            session_id (str): session id to which this node belongs
            run_id (int): run id to which this node belongs
            **kwargs: additional parameters to be passed to self._format_IR(): \
                - required: ["label", "description", "session_id", "run_id"] \
                - additional: follow comments in self._format_IR()
        """
        if node_type not in self._nodes:
            raise ValueError(f"Current context does not have IR of type '{node_type}'.")

        # 兼容旧版state字段
        if node_type == "State" and "state" in kwargs:
            legacy = kwargs.pop("state")
            kwargs.setdefault("content", legacy)
            for _field in (
                "reasoning_content",
                "goal",
                "belief",
                "action_history",
                "current_status",
                "available_actions",
                "feedback",
                "uncentainty",
            ):
                kwargs.setdefault(_field, "")
            logger.info("State field is deprecated. Please use content field instead.")

        IR: BaseIR = self._format_IR(
            node_type=node_type,
            label=label,
            description=description,
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            **kwargs,
        )
        if label not in self._nodes[node_type]:
            self._nodes[node_type][label] = IR
        else:
            raise ValueError("Multiple nodes with same label detected.")

    def modify_IR(self, *, label: str, node_type: str, changes: dict[str, Any]) -> None:
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

    def remove_IR(self, *, label: str, node_type: str) -> None:
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

    def show_data_lineage(self, *, text_file_only: bool = False) -> list[list[tuple[str, None, None]]]:
        """
        Show the lineage of the data ir.

        Args:
            text_file_only (bool): whether to only show text files (Default: `False`)

        Returns:
            list[list[tuple[str, None, None]]], the lineage of the data ir
        """
        groups: dict[str, list[tuple[datetime, tuple[str, None, None]]]] = defaultdict(list)
        for node_type, label, node in self.iter_nodes():
            if node_type not in ("Table", "File", "Script"):
                continue

            raw = getattr(node, "path", None)
            if not raw or not isinstance(raw, str):
                continue

            if text_file_only and not is_text_file(filepath=raw):
                continue

            key = lineage_path_key(p=raw)
            groups[key].append((node.created_at, (f"{node_type}({label})", None, None)))

        # sort outer list by earliest created_at timestamp
        def earliest(ts: list[tuple[datetime, tuple[str, None, None]]]) -> datetime:
            return min(t for t, _ in ts)

        ordered_keys = sorted(groups.keys(), key=lambda k: earliest(groups[k]))
        out: list[list[tuple[str, None, None]]] = []
        for k in ordered_keys:
            items = sorted(groups[k], key=lambda x: x[0], reverse=True)
            out.append([s for _, s in items])

        return out
