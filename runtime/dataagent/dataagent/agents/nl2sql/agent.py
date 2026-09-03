"""Native LangGraph implementation of the NL2SQL agent and Deep Agents subgraph."""

from __future__ import annotations

import csv
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict
from io import StringIO
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Checkpointer

from dataagent.agents.nl2sql.nodes import (
    BaseNL2SQLNode,
    BusinessTwinPerceptorNode,
    ExecutorNode,
    GeneratorNode,
    PerceptorNode,
    ReflectorNode,
    SelectorNode,
    TrafficInsightPerceptorNode,
    ValidatorNode,
)
from dataagent.agents.nl2sql.workflow.router import NL2SQLRouter
from dataagent.agents.nl2sql.workflow.state import (
    NL2SQLInputState,
    NL2SQLOutputState,
    NL2SQLState,
    NL2SQLStructuredResult,
    get_default_state,
)
from dataagent.core.errors import DataAgentError
from dataagent.utils.constants import DEFAULT_NL2SQL_REF_RETRIES, DEFAULT_NL2SQL_SEL_RETRIES

_PIPELINE_NODE_NAMES = ("perceptor", "generator", "validator", "reflector", "executor", "selector")


class NL2SQLAgent:
    """Compatibility facade over the canonical native NL2SQL compiled graph."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        nodes: Sequence[BaseNL2SQLNode],
        config: Mapping[str, Any],
    ) -> None:
        self.graph = graph
        self.nodes = list(nodes)
        self.config = dict(config)
        self.backend = "langgraph"
        self.sql_security_enabled = any(
            isinstance(node, ValidatorNode) and node.sql_security_enabled for node in self.nodes
        )

    @staticmethod
    def _raw_config(config: Any) -> dict[str, Any]:
        if hasattr(config, "get_all"):
            raw = config.get_all()
        elif isinstance(config, Mapping):
            raw = dict(config)
        else:
            raise TypeError("NL2SQL config must be a mapping or ConfigManager-compatible object.")
        return dict(raw)

    @classmethod
    def from_config(
        cls,
        config: Any,
        config_manager: Any | None = None,
        *,
        model: BaseChatModel | None = None,
        backend: BackendProtocol | None = None,
    ) -> NL2SQLAgent:
        """Build the compatibility facade using native LangChain and LangGraph components."""
        from dataagent.core.deepagents.config.workspace import WorkspaceConfigCompiler

        raw_config = cls._raw_config(config_manager or config)
        _validate_pipeline_config(raw_config)
        resolved_model = model or _compile_standalone_model(raw_config)
        resolved_backend = backend or WorkspaceConfigCompiler(raw_config).compile().backend
        graph, nodes = _build_nl2sql_graph(raw_config, resolved_model, resolved_backend)
        return cls(graph=graph, nodes=nodes, config=raw_config)

    async def chat(
        self,
        message: str,
        initial_state: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Invoke the native NL2SQL graph through the former standalone chat surface."""
        state = _prepare_standalone_input(message, initial_state)
        config = _standalone_run_config(kwargs.get("session_id"), kwargs.get("checkpoint_id"))
        return await self.graph.ainvoke(state, config=config)

    def astream(
        self,
        input: Any = None,
        *,
        initial_state: Mapping[str, Any] | None = None,
        message: str | None = None,
        session_id: str | None = None,
        checkpoint_id: str | None = None,
        stream_mode: Any = "values",
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Stream native LangGraph output through the former standalone surface."""
        source = initial_state if initial_state is not None else input if isinstance(input, Mapping) else None
        query = message or (input if isinstance(input, str) else "")
        state = _prepare_standalone_input(str(query or ""), source)
        config = _standalone_run_config(session_id, checkpoint_id)
        return self.graph.astream(state, config=config, stream_mode=stream_mode, **kwargs)


def create_nl2sql_agent(
    config: Mapping[str, Any],
    model: BaseChatModel,
    backend: BackendProtocol,
    *,
    name: str = "nl2sql",
    checkpointer: Checkpointer = None,
    store: BaseStore | None = None,
    debug: bool = False,
) -> CompiledStateGraph:
    """Compile the deterministic NL2SQL pipeline as a native LangGraph graph."""
    graph, _ = _build_nl2sql_graph(
        config,
        model,
        backend,
        name=name,
        checkpointer=checkpointer,
        store=store,
        debug=debug,
    )
    return graph


def _build_nl2sql_graph(
    config: Mapping[str, Any],
    model: BaseChatModel,
    backend: BackendProtocol,
    *,
    name: str = "nl2sql",
    checkpointer: Checkpointer = None,
    store: BaseStore | None = None,
    debug: bool = False,
) -> tuple[CompiledStateGraph, list[BaseNL2SQLNode]]:
    nodes, state_defaults = _build_nodes(config, model)
    router = NL2SQLRouter(enabled_nodes=_PIPELINE_NODE_NAMES)
    dialect = str(_mapping(config.get("DATABASE")).get("dialect", "sqlite") or "sqlite")
    builder = StateGraph(
        NL2SQLState,
        input_schema=NL2SQLInputState,
        output_schema=NL2SQLOutputState,
    )

    async def prepare(state: NL2SQLInputState) -> dict[str, Any]:
        question = _resolve_question(state)
        initialized = get_default_state(question, **state_defaults)
        return {
            key: value for key, value in initialized.items() if key not in {"messages", "files", "structured_response"}
        }

    async def finalize(state: NL2SQLState) -> dict[str, Any]:
        return await _finalize_result(state, backend, dialect)

    builder.add_node("prepare", prepare)
    for node in nodes:
        builder.add_node(node.name, node.aprocess)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", router.entry_point)
    builder.add_edge("perceptor", "generator")
    builder.add_edge("generator", "validator")
    builder.add_edge("validator", "reflector")
    builder.add_conditional_edges("reflector", router.route_from_reflector, ["validator", "executor"])
    builder.add_edge("executor", "selector")
    builder.add_conditional_edges("selector", _route_after_selector, ["reflector", "finalize"])
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer, store=store, debug=debug, name=name), nodes


def _build_nodes(
    config: Mapping[str, Any],
    model: BaseChatModel,
) -> tuple[list[BaseNL2SQLNode], dict[str, Any]]:
    core_config = _validate_pipeline_config(config)
    database_config = _mapping(config.get("DATABASE"))
    perceptor_class = _resolve_perceptor_class(database_config)
    validator_config = dict(_mapping(core_config.get("validator")))
    security_enabled = bool(validator_config.get("sql_security_enabled", False))
    reflector_config = dict(_mapping(core_config.get("reflector")))
    if security_enabled:
        reflector_config.update({"sql_security_enabled": True})

    node_specs: tuple[tuple[type[BaseNL2SQLNode], Mapping[str, Any]], ...] = (
        (perceptor_class, _mapping(core_config.get("perceptor"))),
        (GeneratorNode, _mapping(core_config.get("generator"))),
        (ValidatorNode, validator_config),
        (ReflectorNode, reflector_config),
        (ExecutorNode, _mapping(core_config.get("executor"))),
        (SelectorNode, _mapping(core_config.get("selector"))),
    )
    nodes: list[BaseNL2SQLNode] = [
        node_class(model=model, agent_config=config, **dict(node_config)) for node_class, node_config in node_specs
    ]
    state_defaults = {
        "ref_retries": reflector_config.get("ref_retries", DEFAULT_NL2SQL_REF_RETRIES),
        "sel_retries": _mapping(core_config.get("selector")).get("sel_retries", DEFAULT_NL2SQL_SEL_RETRIES),
    }
    return nodes, state_defaults


def _validate_pipeline_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    core_config = _mapping(config.get("CORE"))
    missing = [node_name for node_name in _PIPELINE_NODE_NAMES if node_name not in core_config]
    if missing:
        raise ValueError(f"NL2SQL.CORE must configure all pipeline nodes; missing: {', '.join(missing)}.")
    return core_config


def _resolve_perceptor_class(database_config: Mapping[str, Any]) -> type[PerceptorNode]:
    perceptor_classes: dict[str, type[PerceptorNode]] = {
        "business_twin": BusinessTwinPerceptorNode,
        "traffic_insight": TrafficInsightPerceptorNode,
    }
    perceptor_type = str(database_config.get("perceptor_type", "") or "").strip().lower()
    return perceptor_classes.get(perceptor_type, PerceptorNode)


def _route_after_selector(state: NL2SQLState) -> str:
    return "finalize" if state.get("proceed", False) else "reflector"


async def _finalize_result(state: NL2SQLState, backend: BackendProtocol, dialect: str) -> dict[str, Any]:
    error = state.get("error")
    if error:
        raise DataAgentError(source="tool", component="nl2sql", fact=str(error))
    sql = str(state.get("sql", "") or "").strip()
    columns = [str(column) for column in (state.get("columns") or [])]
    rows = list(state.get("rows") or [])
    if not sql:
        raise DataAgentError(source="internal", component="nl2sql", fact="NL2SQL completed without SQL.")

    invocation_id = uuid.uuid4().hex
    sql_path = f"/nl2sql/{invocation_id}/query.sql"
    csv_path = f"/nl2sql/{invocation_id}/result.csv"
    formatted_sql = _format_sql(sql, dialect)
    csv_content = _render_csv(columns, rows)
    await _write_artifact(backend, sql_path, f"{formatted_sql.rstrip(';')};\n")
    await _write_artifact(backend, csv_path, csv_content)

    preview = [_json_row(row) for row in (state.get("rows_preview") or [])]
    structured_result = NL2SQLStructuredResult(
        sql=formatted_sql,
        sql_path=sql_path,
        csv_path=csv_path,
        columns=columns,
        row_count=len(rows),
        rows_preview=preview,
        confidence=float(state.get("confidence", 0.0) or 0.0),
        error=None,
    )
    content = json.dumps(asdict(structured_result), ensure_ascii=False, default=str)
    return {"messages": [AIMessage(content=content)], "structured_response": structured_result}


async def _write_artifact(backend: BackendProtocol, path: str, content: str) -> None:
    result = await backend.awrite(path, content)
    error = getattr(result, "error", None)
    if error:
        raise DataAgentError(source="tool", component="nl2sql", fact=f"Failed to write {path}: {error}")


def _resolve_question(state: Mapping[str, Any]) -> str:
    explicit = str(state.get("question", "") or "").strip()
    if explicit:
        return explicit
    messages = state.get("messages", [])
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                text = _message_text(message.content).strip()
                if text:
                    return text
    raise ValueError("NL2SQL requires a question or at least one HumanMessage.")


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _render_csv(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    if columns:
        writer.writerow(columns)
    writer.writerows(rows)
    return "\ufeff" + output.getvalue()


def _json_row(row: Sequence[Any]) -> list[Any]:
    return [value if value is None or isinstance(value, (bool, int, float, str)) else str(value) for value in row]


def _format_sql(sql: str, dialect: str) -> str:
    try:
        import sqlglot

        parsed = sqlglot.parse_one(sql, read=dialect or None)
        return parsed.sql(dialect=dialect or None, pretty=True)
    except Exception:
        return sql.strip().rstrip(";")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _compile_standalone_model(config: Mapping[str, Any]) -> BaseChatModel:
    from dataagent.core.deepagents.config.models import ModelConfigCompiler

    compiler = ModelConfigCompiler(config)
    models = compiler.compile()
    primary_name = compiler.resolve_primary_model_name(models)
    model = models.get(primary_name)
    if model is None:
        raise ValueError(f"Primary model '{primary_name}' is not available.")
    return model


def _prepare_standalone_input(message: str, initial_state: Mapping[str, Any] | None) -> dict[str, Any]:
    state = dict(initial_state or {})
    messages = list(state.get("messages", []))
    if message.strip():
        messages.append(HumanMessage(content=message))
    state.update({"messages": messages})
    return state


def _standalone_run_config(session_id: Any, checkpoint_id: Any) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": str(session_id or uuid.uuid4())}
    if checkpoint_id:
        configurable.update({"checkpoint_id": str(checkpoint_id)})
    return {"configurable": configurable}


__all__ = ["NL2SQLAgent", "NL2SQLStructuredResult", "create_nl2sql_agent"]
