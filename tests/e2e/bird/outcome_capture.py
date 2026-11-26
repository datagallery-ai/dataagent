"""Observation-only hooks for new 97da-compatible BIRD evaluator bundles."""

import ast
import hashlib
import inspect
import json
import os
import textwrap
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_current = ContextVar("bird_outcome_identity", default={})


def capture():
    from artifact_capture import _current as current_case

    return current_case.get()


def emit(event, **values):
    """Never let a diagnostic failure change the product outcome."""
    cap = capture()
    if cap is None:
        return
    try:
        root = cap.directory.parent / "observation-v1"
        root.mkdir(mode=0o700, exist_ok=True)
        row = {
            "version": 1,
            "case_id": cap.case_id,
            "monotonic": time.monotonic(),
            "node": None,
            "candidate_id": None,
            "strategy": None,
            "slot_id": None,
            "checker": None,
            **_current.get(),
            "event": event,
            **values,
        }
        raw = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        with os.fdopen(
            os.open(root / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a"
        ) as f:
            f.write(raw)
    except Exception as exc:
        cap.capture_errors.append(
            {"operation": "outcome_capture", "error_type": type(exc).__name__}
        )


def sql_ref(sql):
    if not isinstance(sql, str):
        return None
    cap = capture()
    if cap is None:
        return None
    try:
        raw = sql.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        parent = cap.directory.parent / "observation-v1"
        parent.mkdir(mode=0o700, exist_ok=True)
        root = parent / "blobs"
        root.mkdir(mode=0o700, exist_ok=True)
        path = root / (sha + ".sql")
        if not path.exists():
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                f.write(raw)
        return {"sha256": sha, "path": str(path), "bytes": len(raw)}
    except Exception as exc:
        cap.capture_errors.append({"operation": "sql_ref", "error_type": type(exc).__name__})
        return None


@contextmanager
def scope(**identity):
    token = _current.set({**_current.get(), **identity})
    try:
        yield
    finally:
        _current.reset(token)


def condition(value, line, expression):
    result = bool(value)
    emit("checker_condition", source_line=line, expression=expression, value=result)
    return result


def record_model_call(logical_call_id):
    emit("model_call", logical_call_id=logical_call_id)


def instrument(fn, kind):
    """Compile the same function with observations, preserving its original globals."""
    lines, first = inspect.getsourcelines(fn)
    tree = ast.parse(textwrap.dedent("".join(lines)))
    top = tree.body[0]
    top.decorator_list = []
    if kind == "candidate":
        matches = [
            n for n in ast.walk(top) if isinstance(n, ast.AsyncFunctionDef) and n.name == "validate"
        ]
        if len(matches) != 1:
            raise ValueError("Unsupported BIRD candidate function")
        n = matches[0]
        wrapper = ast.parse(
            "with _bird_obs.scope(node='bird-validator', candidate_id=candidate.id, strategy=candidate.strategy, candidate_input_sha256=_bird_obs.sql_ref(candidate.sql)):\n pass"
        ).body[0]
        wrapper.body = n.body
        n.body = [wrapper]
    elif kind == "candidate_mapping":
        found = False
        for index, n in enumerate(top.body):
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "candidates" for t in n.targets
            ):
                top.body.insert(
                    index + 1, ast.parse("_bird_obs.map_candidates(results, candidates)").body[0]
                )
                found = True
                break
        if not found:
            raise ValueError("Unsupported Generator candidate assignment")
    elif kind == "attempt":
        loops = [
            n
            for n in ast.walk(top)
            if isinstance(n, ast.For)
            and isinstance(n.target, ast.Name)
            and n.target.id == "attempt"
        ]
        if len(loops) != 1:
            raise ValueError("Unsupported Generator attempt loop")
        n = loops[0]
        for j, statement in enumerate(n.body):
            if isinstance(statement, ast.Return):
                n.body.insert(
                    j,
                    ast.parse(
                        "_bird_obs.emit('generator_accepted', attempt_index=attempt+1, output_refs=[_bird_obs.sql_ref(sql) for sql in sqls])"
                    ).body[0],
                )
                break
        wrapper = ast.parse(
            "with _bird_obs.scope(attempt_index=attempt+1):\n _bird_obs.emit('generator_attempt', attempt_index=attempt+1)"
        ).body[0]
        wrapper.body += n.body
        n.body = [wrapper]
    elif kind == "conditions":

        class Conditions(ast.NodeTransformer):
            def visit_If(self, n):
                self.generic_visit(n)
                expr = ast.unparse(n.test)
                n.test = ast.Call(
                    func=ast.Attribute(ast.Name("_bird_obs", ast.Load()), "condition", ast.Load()),
                    args=[n.test, ast.Constant(first + n.lineno - 1), ast.Constant(expr)],
                    keywords=[],
                )
                return n

        tree = Conditions().visit(tree)
    ast.fix_missing_locations(tree)
    import sys

    ns = dict(fn.__globals__)
    ns["_bird_obs"] = sys.modules[__name__]
    exec(compile(tree, inspect.getsourcefile(fn), "exec"), ns)
    rebuilt = ns[fn.__name__]
    rebuilt.__defaults__ = fn.__defaults__
    rebuilt.__kwdefaults__ = fn.__kwdefaults__
    return rebuilt


def map_candidates(results, candidates):
    cap = capture()
    links = getattr(cap, "_outcome_links", {}) if cap else {}
    for item, candidate in zip(results, candidates):
        identity = links.get(id(item))
        with scope(**(identity or {})):
            emit(
                "generator_candidate_mapping",
                candidate_id=candidate.id,
                identity_known=identity is not None,
                output_ref=sql_ref(candidate.sql),
            )


def install_classes(generator, validator):
    if getattr(generator, "_bird_outcome_v1", False):
        return
    generator._bird_outcome_v1 = True
    original = instrument(generator.generate_with_llm, "attempt")

    @wraps(original)
    async def generate(self, strategy, settings, context):
        cap = capture()
        slot = None
        if cap is not None:
            counters = getattr(cap, "_outcome_slots", {})
            key = (cap.generator_round, strategy)
            counters[key] = counters.get(key, 0) + 1
            cap._outcome_slots = counters
            slot = counters[key]
        with scope(
            node="generator", strategy=strategy, slot_id=slot, invocation_id=uuid.uuid4().hex
        ):
            emit("generator_enter")
            try:
                result = await original(self, strategy, settings, context)
            except BaseException as exc:
                emit("generator_error", error_type=type(exc).__name__)
                raise
            if cap is not None:
                links = getattr(cap, "_outcome_links", {})
                for item in result:
                    links[id(item)] = dict(_current.get())
                cap._outcome_links = links
            emit("generator_result", output_refs=[sql_ref(x[0]) for x in result])
            return result

    generator.generate_with_llm = generate
    generator._aprocess = instrument(generator._aprocess, "candidate_mapping")
    # Rebuilt function uses a private globals dict; wrap its exact stream collector.
    collect = original.__globals__["collect_model_stream"]

    @wraps(collect)
    async def collect_reply(*args, **kwargs):
        result = await collect(*args, **kwargs)
        body = result.content if isinstance(result.content, str) else ""
        emit(
            "model_reply",
            finish_reason=result.response_metadata.get("finish_reason"),
            content_chars=len(body),
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
        )
        return result

    original.__globals__["collect_model_stream"] = collect_reply
    validator._aprocess = instrument(validator._aprocess, "candidate")
    dispatch = validator._aprocess.__globals__["validate_sql"]
    ns = dispatch.__globals__
    runtime = ns["_CheckerRuntime"]
    # Modules are isolated per graph; each real runtime is bound once.
    for prompt_kind in ("common_prompt", "execution_prompt"):
        original_prompt = getattr(runtime, prompt_kind)

        def make_prompt(fn, kind):
            @wraps(fn)
            def wrapped(self, sql, *args, **kwargs):
                result = fn(self, sql, *args, **kwargs)
                emit("repair_trigger", prompt_kind=kind, input_ref=sql_ref(sql))
                return result

            return wrapped

        setattr(runtime, prompt_kind, make_prompt(original_prompt, prompt_kind))
    ask = runtime.ask

    @wraps(ask)
    async def asked(self, prompt, *, count, stop):
        emit("checker_sample_batch", count=count)
        result = await ask(self, prompt, count=count, stop=stop)
        for index, reply in enumerate(result[0]):
            content = reply.content if isinstance(reply.content, str) else ""
            emit(
                "checker_response",
                sample_index=index,
                finish_reason=reply.response_metadata.get("finish_reason"),
                content_chars=len(content),
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            )
        return result

    runtime.ask = asked
    for name, cls in ns["_CHECKERS"].items():
        check = instrument(cls.check_and_revise, "conditions")

        def make_checker(fn, checker_name):
            @wraps(fn)
            async def wrapped(self, sql, *args, **kwargs):
                with scope(checker=checker_name, checker_invocation_id=uuid.uuid4().hex):
                    emit("checker_enter", input_ref=sql_ref(sql))
                    try:
                        result = await fn(self, sql, *args, **kwargs)
                    except BaseException as exc:
                        emit(
                            "checker_error", error_type=type(exc).__name__, adopted_output_ref=None
                        )
                        raise
                    emit(
                        "checker_exit",
                        adopted_output_ref=sql_ref(result[0]),
                        changed=result[0] != sql,
                    )
                    return result

            return wrapped

        cls.check_and_revise = make_checker(check, name)
        parser = cls._parse_llm_response

        def make_parser(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                result = fn(*args, **kwargs)
                emit(
                    "checker_parse",
                    accepted=bool(result),
                    output_ref=sql_ref(result) if result else None,
                )
                return result

            return wrapped

        cls._parse_llm_response = staticmethod(make_parser(parser))


def install():
    from dataagent.extensions.loading import PythonLoader

    original = PythonLoader._exec_module
    if getattr(original, "_bird_outcome_v1", False):
        return

    @wraps(original)
    def execute(self, root, path):
        module = original(self, root, path)
        build = vars(module).get("build_nl2sql_graph")
        if callable(build):
            ns = build.__globals__
            generator = ns.get("GeneratorNode")
            validator = ns.get("BirdValidatorNode")
            if generator is not None and validator is not None:
                install_classes(generator, validator)
        return module

    execute._bird_outcome_v1 = True
    PythonLoader._exec_module = execute
