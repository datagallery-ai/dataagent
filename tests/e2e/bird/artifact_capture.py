"""Experiment-only NL2SQL state capture; does not replace schema acquisition.

Call ``install_capture_hooks()`` after loading the NL2SQL plugin, then put each
case invocation inside ``with capture_case(case_directory, case_id=...) as log``.
Call ``log.record_final(payload)`` if the public result payload is available.
Each case directory must be unique (including retry attempts).

Private state dumps retain schema, candidate SQL and full execution rows for vote audit.
Model prompts, reasoning and connection settings are not serialized. Never print dumps in monitoring.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

_current: ContextVar[CaseCapture | None] = ContextVar("bird_artifact_capture", default=None)
_RESULT_FIELDS = (
    "columns",
    "rows",
    "rows_preview",
    "row_count",
    "total_known",
    "truncated",
    "issues_by_source",
    "security_checked",
    "security_violations",
    "id",
    "sql",
    "strategy",
    "score",
    "confidence",
    "issues",
    "error",
)
_STATE_FIELDS = (
    "result_repair_attempts",
    "result_repair_pending",
    "columns",
    "rows",
    "rows_preview",
    "row_count",
    "total_known",
    "truncated",
    "execution_durations",
    "sql",
    "confidence",
    "issues",
    "error",
    "keywords",
    "schema",
    "joins",
    "schema_str",
    "review_history",
    "question",
    "evidence",
    "few_shot_examples",
    "sql_rules",
    "ref_retries",
    "sel_retries",
    "proceed",
    "security_sql_approved",
)
_CANDIDATE_FIELDS = ("generation_results", "validation_results", "execution_results")


def _result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {key: value[key] for key in _RESULT_FIELDS if key in value}
    return {key: getattr(value, key) for key in _RESULT_FIELDS if hasattr(value, key)}


def _snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: state[key] for key in _STATE_FIELDS if key in state}
    for key in _CANDIDATE_FIELDS:
        if key in state:
            result[key] = [_result(item) for item in (state[key] or [])]
    return result


def _observe(capture, method: str, *args, **kwargs) -> None:
    try:
        getattr(capture, method)(*args, **kwargs)
    except Exception as exc:
        capture.capture_errors.append({"operation": method, "error_type": type(exc).__name__})


class CaseCapture:
    def __init__(self, directory: str | Path, *, case_id: str, secrets: tuple[str, ...] = ()):
        self.directory = Path(directory) / "nl2sql_context_dump"
        self.directory.mkdir(parents=True, exist_ok=False)
        self.case_id = case_id
        self.secrets = tuple(value for value in secrets if value)
        self.node_index = 0
        self.generation_index = 0
        self.validation_round = 0
        self.generator_round = 0
        self.generation_started = 0
        self.strategy_slots: dict[tuple[int, str], int] = {}
        self.last_node: str | None = None
        self.last_state: dict[str, Any] = {}
        self.final_payload: dict[str, Any] | None = None
        self.capture_errors: list[dict[str, str]] = []

    def _encode(self, payload: Any) -> str:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
        for secret in self.secrets:
            raw = raw.replace(secret, "[REDACTED]")
        return raw

    def _write(self, name: str, payload: Any, *, append: bool = False) -> None:
        try:
            with (self.directory / name).open("a" if append else "w", encoding="utf-8") as file:
                file.write(self._encode(payload) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            # A diagnostics failure must not alter the model/algorithm outcome.
            self.capture_errors.append({"file": name, "error_type": type(exc).__name__})

    def record_checker(self, payload: dict[str, Any]) -> None:
        self._write("checker-events.jsonl", {"case_id": self.case_id, **payload}, append=True)

    def begin_node(self, name: str, state: Mapping[str, Any]) -> int:
        self.node_index += 1
        self._write(
            f"{self.node_index:03d}-{name}-input.json",
            {
                "case_id": self.case_id,
                "index": self.node_index,
                "node": name,
                "phase": "input",
                "generator_round": self.generator_round,
                "validation_round": self.validation_round + int(name == "validator"),
                "state": _snapshot(state),
            },
        )
        return self.node_index

    def record_node(
        self,
        name: str,
        state: Mapping[str, Any],
        *,
        error: BaseException | None = None,
        invocation_index: int | None = None,
    ) -> None:
        if invocation_index is None:
            self.node_index += 1
            invocation_index = self.node_index
        # Materialize now: later reflector passes may mutate candidate objects.
        snapshot = json.loads(self._encode(_snapshot(state)))
        self.last_node, self.last_state = name, snapshot
        payload = {
            "case_id": self.case_id,
            "index": invocation_index,
            "node": name,
            "phase": "output",
            "generator_round": self.generator_round,
            "validation_round": self.validation_round,
            "status": "error" if error is not None else "completed",
            "error_type": type(error).__name__ if error is not None else None,
            "state": snapshot,
        }
        self._write(f"{invocation_index:03d}-{name}.json", payload)
        self._write("nodes.jsonl", payload, append=True)
        if name == "perceptor" and error is None:
            schema_payload = {
                "case_id": self.case_id,
                "node_index": invocation_index,
                "schema": snapshot.get("schema", {}),
                "joins": snapshot.get("joins", []),
                "schema_str": snapshot.get("schema_str", ""),
                "tables": sorted(snapshot.get("schema", {})),
                "columns": sorted(
                    f"{table}.{column}"
                    for table, definition in snapshot.get("schema", {}).items()
                    for column in definition.get("columns", {})
                ),
            }
            self._write("schema_linking.json", schema_payload)
            self._write("schema_linking.jsonl", schema_payload, append=True)

    def begin_generation(self, strategy: str) -> dict[str, int]:
        self.generation_started += 1
        key = (self.generator_round, strategy)
        self.strategy_slots[key] = self.strategy_slots.get(key, 0) + 1
        return {
            "invocation_id": self.generation_started,
            "round": self.generator_round,
            "slot": self.strategy_slots[key],
        }

    def record_generation(
        self,
        strategy: str,
        candidates: Any,
        *,
        identity: dict[str, int],
        error: BaseException | None = None,
    ) -> None:
        self.generation_index += 1
        self._write(
            "generator_sql.jsonl",
            {
                "case_id": self.case_id,
                "generation_index": self.generation_index,
                "strategy": strategy,
                **identity,
                "status": "error" if error is not None else "completed",
                "error_type": type(error).__name__ if error is not None else None,
                "sqls": [item[0] for item in candidates] if candidates is not None else [],
            },
            append=True,
        )

    def record_final(self, payload: Mapping[str, Any]) -> None:
        """Persist the public NL2SQL result, without rows or prompts."""
        self.final_payload = {
            key: payload[key]
            for key in ("sql", "confidence", "error", "issues", "executed")
            if key in payload
        }
        self._write("final_result.json", {"case_id": self.case_id, **self.final_payload})

    def finish(self, error: BaseException | None) -> None:
        self._write(
            "final_state.json",
            {
                "case_id": self.case_id,
                "last_node": self.last_node,
                "state": self.last_state,
                "public_result": self.final_payload,
                "invocation_error_type": type(error).__name__ if error is not None else None,
                "node_events": self.node_index,
                "generator_calls": self.generation_index,
                "capture_errors": self.capture_errors,
            },
        )


@contextmanager
def capture_case(directory: str | Path, *, case_id: str, secrets: tuple[str, ...] = ()):
    capture = CaseCapture(directory, case_id=case_id, secrets=secrets)
    token = _current.set(capture)
    error = None
    try:
        yield capture
    except BaseException as exc:
        error = exc
        raise
    finally:
        _observe(capture, "finish", error)
        _current.reset(token)


def install_capture_hooks(*, base_node_class=None, generator_class=None) -> None:
    """Install transparent observers once; injectable classes enable offline tests."""
    if base_node_class is None:
        from nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode

        base_node_class = BaseNL2SQLNode
    if generator_class is None:
        from nl2sql.nodes.generator import GeneratorNode

        generator_class = GeneratorNode

    original_run = base_node_class.run
    if not getattr(original_run, "_bird_capture", False):

        @wraps(original_run)
        async def run(self, state, *args, **kwargs):
            capture = _current.get()
            if capture is not None and self.name == "generator":
                capture.generator_round += 1
            invocation_index = None
            if capture is not None:
                try:
                    invocation_index = capture.begin_node(self.name, state)
                except Exception as exc:
                    capture.capture_errors.append(
                        {"operation": "begin_node", "error_type": type(exc).__name__}
                    )
            try:
                result = await original_run(self, state, *args, **kwargs)
            except BaseException as exc:
                if capture is not None:
                    _observe(
                        capture,
                        "record_node",
                        self.name,
                        state,
                        error=exc,
                        invocation_index=invocation_index,
                    )
                raise
            if capture is not None:
                _observe(
                    capture, "record_node", self.name, result, invocation_index=invocation_index
                )
            return result

        run._bird_capture = True
        base_node_class.run = run

    original_generate = generator_class.generate_with_llm
    if not getattr(original_generate, "_bird_capture", False):

        @wraps(original_generate)
        async def generate(self, strategy, settings, context, *args, **kwargs):
            capture = _current.get()
            identity = capture.begin_generation(strategy) if capture is not None else {}
            try:
                result = await original_generate(self, strategy, settings, context, *args, **kwargs)
            except BaseException as exc:
                if capture is not None:
                    _observe(
                        capture, "record_generation", strategy, None, identity=identity, error=exc
                    )
                raise
            if capture is not None:
                _observe(capture, "record_generation", strategy, result, identity=identity)
            return result

        generate._bird_capture = True
        generator_class.generate_with_llm = generate


def install_plugin_capture_hooks() -> None:
    """Observe each isolated plugin's actual classes before its factory builds a graph.

    Do not import plain ``nl2sql``: PythonLoader intentionally gives every
    compilation its own module graph. Its entrypoint imports the graph builder
    eagerly, so its globals identify the exact classes that factory will use.
    """
    from dataagent.extensions.loading import PythonLoader

    original = PythonLoader._exec_module
    if getattr(original, "_bird_capture_loader", False):
        return

    @wraps(original)
    def execute(self, root, path):
        module = original(self, root, path)
        build_graph = vars(module).get("build_nl2sql_graph")
        if callable(build_graph):
            namespace = getattr(build_graph, "__globals__", {})
            perceptor = namespace.get("PerceptorNode")
            generator = namespace.get("GeneratorNode")
            if perceptor is not None and generator is not None:
                base = next(cls for cls in perceptor.__mro__ if cls.__name__ == "BaseNL2SQLNode")
                install_capture_hooks(base_node_class=base, generator_class=generator)
                from vote_only import install

                install(namespace["SelectorNode"])
        return module

    execute._bird_capture_loader = True
    PythonLoader._exec_module = execute
