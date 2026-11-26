"""Evaluation execution evidence and explicit simulated-user interrupt handling."""

import asyncio
import json
import re
import shlex
from pathlib import Path


def timeout_callback():
    from langchain_core.callbacks import AsyncCallbackHandler

    class ModelTimeoutTracker(AsyncCallbackHandler):
        def __init__(self):
            self.active = {}
            self.cancelled_model = None

        async def on_chat_model_start(
            self, serialized, messages, *, run_id, metadata=None, **kwargs
        ):
            meta = metadata or {}
            node = meta.get("langgraph_node", "model")
            model = meta.get("ls_model_name") or kwargs.get("name") or "chat"
            self.active[run_id] = f"{node} LLM {model}"

        async def on_llm_end(self, response, *, run_id, **kwargs):
            self.active.pop(run_id, None)

        async def on_llm_error(self, error, *, run_id, **kwargs):
            model = self.active.pop(run_id, None)
            if model and isinstance(error, asyncio.CancelledError):
                self.cancelled_model = model

    return ModelTimeoutTracker()


async def invoke_with_feedback(agent, query, thread_id, responses, transcript, *, progress=None):
    """Run/resume one graph; the caller owns a single deadline for the entire case."""
    from langgraph.types import Command

    progress = progress or timeout_callback()
    config = {"configurable": {"thread_id": thread_id}, "callbacks": [progress]}
    inputs = {
        "messages": [
            (
                "system",
                "当需要向用户确认时，必须调用 `data-qa__request_human_feedback` 工具获取用户回复。仅在最终回答中提问无法完成确认。",
            ),
            ("user", query),
        ]
    }
    consumed = 0
    while True:
        state = await agent.ainvoke(inputs, config)
        pending = state.get("__interrupt__", ())
        if not pending:
            return state
        resumes = {}
        for item in pending:
            payload = item.value
            entry = {"interrupt_id": item.id, "payload": payload, "consumed": False}
            transcript.append(entry)
            if not isinstance(payload, dict) or payload.get("kind") != "human_feedback":
                raise RuntimeError(
                    "Unsupported non-feedback interrupt; no automatic approval supplied"
                )
            entry["question"] = payload.get("question", "")
            if consumed >= len(responses):
                # Match the legacy input mock: exhausted queues return an empty reply.
                entry.update(answer="", response_index=None, queue_exhausted=True)
            else:
                entry.update(answer=responses[consumed], response_index=consumed, consumed=True)
                consumed += 1
            resumes[item.id] = entry["answer"]
        inputs = Command(resume=resumes)


def text(content):
    if isinstance(content, str):
        return content
    return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))


def _normalize_skill_cd(tokens, skill_root):
    """Recognize only cd <installed skill> && python scripts/<that skill's script>."""
    if skill_root is None or len(tokens) < 5 or tokens[0] != "cd" or tokens[2] != "&&":
        return tokens
    if tokens.count("&&") != 1 or any(
        token in {";", "||", "|", ">", "2>", "&", "<", ">>", "<<"} for token in tokens
    ):
        return tokens
    if any(any(char in token for char in ("$", "`", "\n")) for token in tokens):
        return tokens
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", Path(tokens[3]).name):
        return tokens
    skill_scripts = {
        "create-neutralization-experiment": "insert_neutralization_experiment.py",
        "check-inhibitor-sample-for-neutralization-experiment": "check_inhibitor_sample_sufficiency.py",
        "assign-researcher-for-neutralization-experiment": "assign_experiment_researcher.py",
    }
    directory = Path(tokens[1])
    root = Path(skill_root).resolve()
    if not directory.is_absolute() or directory.parent.resolve() != root:
        return tokens
    script = skill_scripts.get(directory.name)
    if not script or tokens[4] != f"scripts/{script}":
        return tokens
    resolved = (directory / "scripts" / script).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return tokens
    return [tokens[3], str(resolved), *tokens[5:]]


def collect_shell_evidence(messages, *, skill_root=None):
    """Only matched execute replies with successful exit and substantive output qualify."""
    calls = {}
    call_indices = {}
    evidence = []
    for index, message in enumerate(messages):
        if getattr(message, "type", None) == "ai":
            for call in getattr(message, "tool_calls", []):
                if call.get("name") == "execute":
                    calls[call["id"]] = call
                    call_indices[call["id"]] = index
        if (
            getattr(message, "type", None) != "tool"
            or getattr(message, "tool_call_id", None) not in calls
        ):
            continue
        call = calls[message.tool_call_id]
        output = text(message.content)
        artifact = getattr(message, "artifact", None)
        exit_code = artifact.get("exit_code") if isinstance(artifact, dict) else None
        match = re.search(
            r"\n\[Command (?:succeeded|failed) with exit code (-?\d+)\](?:\n\[Output was truncated due to size limits\])?\s*$",
            output,
        )
        if exit_code is None and match:
            exit_code = int(match.group(1))
        stdout = output[: match.start()].strip() if match else output.strip()
        if stdout == "<no output>":
            stdout = ""
        command = call.get("args", {}).get("command", "")
        category = "other"
        direct_sqlite_read = False
        try:
            # shlex discards newlines, which can hide a second shell command.
            if "\n" in command or "\r" in command:
                raise ValueError("Multiline shell commands are not recognized")
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = []
        tokens = _normalize_skill_cd(tokens, skill_root)
        complex_shell = any(token in {";", "&&", "||", "|", ">", "2>", "&"} for token in tokens)
        scripts = (
            "insert_neutralization_experiment.py",
            "check_inhibitor_sample_sufficiency.py",
            "assign_experiment_researcher.py",
        )
        if tokens and not complex_shell and Path(tokens[0]).name.startswith("python"):
            if (
                len(tokens) >= 2
                and Path(tokens[1]).name in scripts
                and not any(token in {"--help", "-h"} for token in tokens)
            ):
                category = "skill_script"
            elif (
                len(tokens) == 3
                and tokens[1] == "-c"
                and "sqlite3.connect" in tokens[2]
                and re.search(r"\bSELECT\b", tokens[2], re.I)
            ):
                category = "database_read"
        if (
            tokens
            and not complex_shell
            and Path(tokens[0]).name == "sqlite3"
            and len(tokens) >= 3
            and re.match(r"\s*SELECT\b", tokens[-1], re.I)
        ):
            category = "database_read"
            direct_sqlite_read = True
        failure_output = any(
            marker in stdout
            for marker in [
                "Traceback (most recent call last)",
                "Error executing",
                "command not found",
                "No such file or directory",
            ]
        )
        try:
            parsed = json.loads(stdout)
            failure_output = failure_output or (
                isinstance(parsed, dict) and bool(parsed.get("error"))
            )
        except ValueError:
            pass
        successful = (
            exit_code == 0
            and getattr(message, "status", "success") != "error"
            and not failure_output
        )
        evidence.append(
            {
                "call": call,
                "call_message_index": call_indices[message.tool_call_id],
                "reply_message_index": index,
                "exit_code": exit_code,
                "output": stdout,
                "raw_reply": output,
                "successful": successful,
                "category": category,
                "script": Path(tokens[1]).name if category == "skill_script" else None,
                "empty_result": direct_sqlite_read and not stdout,
                "qualifies": successful
                and (bool(stdout) or direct_sqlite_read)
                and category != "other",
            }
        )
    return evidence
