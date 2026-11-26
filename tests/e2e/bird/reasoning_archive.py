"""Future BIRD runs: private, per-physical-attempt reasoning-only archives."""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path


class ReasoningArchive:
    def __init__(self, root, token, context, report):
        self.report = report
        self.enabled = True
        self.closed = False
        self.choices = {}
        self.meta = {
            "schema_version": 1,
            "attempt_id": str(token),
            "pid": os.getpid(),
            "case_id": context.get("case_id"),
            "logical_call_id": context.get("logical_call_id"),
            "started_unix_ns": time.time_ns(),
            "status": "OPEN",
            "choices": {},
            "note": "OPEN after process exit means interrupted/unfinalized; text is delta-concatenated.",
        }
        try:
            root = Path(root)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            self.path = root / (str(os.getpid()) + "-" + uuid.uuid4().hex)
            self.path.mkdir(mode=0o700)
            self._publish()
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        self.enabled = False
        try:
            self.report("reasoning_archive_error", error_type=type(exc).__name__)
        except Exception:
            pass

    def _publish(self):
        self.meta["choices"] = {
            str(k): {
                "characters": v["characters"],
                "chunks": v["chunks"],
                "sha256": v["hash"].hexdigest(),
                "file": f"choice-{k}.reasoning.txt",
            }
            for k, v in self.choices.items()
        }
        tmp = self.path / "metadata.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False, indent=2)
            f.flush()
        os.replace(tmp, self.path / "metadata.json")

    def chunk(self, payload, *, message=False):
        if not self.enabled or self.closed or not isinstance(payload, dict):
            return
        try:
            for choice in payload.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                index = choice.get("index", 0)
                if type(index) is not int or index < 0:
                    continue
                obj = choice.get("message" if message else "delta") or {}
                text = obj.get("reasoning_content") if isinstance(obj, dict) else None
                if not isinstance(text, str) or not text:
                    continue
                info = self.choices.setdefault(
                    index, {"characters": 0, "chunks": 0, "hash": hashlib.sha256()}
                )
                data = text.encode("utf-8")
                fd = os.open(
                    self.path / f"choice-{index}.reasoning.txt",
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                    0o600,
                )
                with os.fdopen(fd, "ab") as f:
                    f.write(data)
                    f.flush()
                info["characters"] += len(text)
                info["chunks"] += 1
                info["hash"].update(data)
            # The allow-list deliberately excludes content, request, headers and error messages.
            usage = payload.get("usage")
            if isinstance(usage, dict):
                clean = {
                    k: v
                    for k, v in usage.items()
                    if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                    and type(v) is int
                }
                details = usage.get("completion_tokens_details")
                if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int:
                    clean["reasoning_tokens"] = details["reasoning_tokens"]
                self.meta["usage"] = clean
            self._publish()
        except Exception as exc:
            self._fail(exc)

    def finish(self, status, *, error_type=None, http_status=None):
        if not self.enabled or self.closed:
            return
        self.closed = True
        try:
            self.meta.update(status=status, ended_unix_ns=time.time_ns())
            if error_type:
                self.meta["error_type"] = error_type
            if http_status is not None:
                self.meta["http_status"] = int(http_status)
            self._publish()
            self.report(
                "reasoning_archive_saved", archive_path=str(self.path), archive_status=status
            )
        except Exception as exc:
            self._fail(exc)


def begin_archive(gate, token, context):
    return ReasoningArchive(
        getattr(gate, "reasoning_archive_directory", Path(gate.directory) / "reasoning-archive"),
        token,
        context,
        lambda event, **fields: gate.record(event, attempt_id=token, **fields),
    )
