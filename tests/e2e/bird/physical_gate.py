"""One-key, multi-process physical HTTP admission for the BIRD experiment."""

import asyncio
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from reasoning_archive import begin_archive


class AdmissionGate:
    def __init__(self, directory, now=time.monotonic, sleep=asyncio.sleep):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.now = now
        self.sleep = sleep
        self.maximum = 8
        self.minimum = 1
        self.window_seconds = 70.0
        self.window_maximum = 50
        self.send_interval = 1.41
        self.penalty_seconds = 1800.0
        boot = Path("/proc/sys/kernel/random/boot_id")
        self.boot_id = boot.read_text().strip() if boot.exists() else None
        # Retain the shared window and independent penalties; redecorate cap 8.
        with self._transaction() as state:
            self._decorate(state)

    def _new(self):
        return {
            "boot_id": self.boot_id,
            "active_tokens": {},
            "admission_times_monotonic": [],
            "penalty_expiry_monotonic": [],
            "last_sent_monotonic": None,
            "physical_attempts": 0,
        }

    @staticmethod
    def _alive(pid):
        try:
            os.kill(pid, 0)
            stat = Path("/proc") / str(pid) / "stat"
            return not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except (ProcessLookupError, FileNotFoundError):
            return False
        except PermissionError:
            return True

    def _decorate(self, state):
        state.update(
            pid=os.getpid(),
            active=len(state["active_tokens"]),
            maximum=self.maximum,
            effective_limit=max(
                self.minimum, self.maximum - len(state["penalty_expiry_monotonic"])
            ),
            window_seconds=self.window_seconds,
            window_maximum=self.window_maximum,
            send_interval_seconds=self.send_interval,
            recent_admissions=len(state["admission_times_monotonic"]),
            monotonic_seconds=self.now(),
            shared_across_workers=True,
        )

    def _event(self, state, event, **values):
        self._decorate(state)
        state["event"] = event
        # Intentionally no request bodies, URLs, headers, or credentials.
        record = {**state, **values}
        with (self.directory / "admission-events.jsonl").open("a") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    @contextmanager
    def _transaction(self):
        with (self.directory / "admission.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.directory / "admission-state.json"
            state = json.loads(path.read_text()) if path.exists() else self._new()
            if self.boot_id and state.get("boot_id") != self.boot_id:
                state = self._new()
            # Migration must occur only after the old worker is confirmed exited.
            if "active_tokens" not in state:
                assert state.get("active", 0) == 0, "Clear old occupancy only after confirming exit"
                state["active_tokens"] = {}
            before = json.dumps(state, sort_keys=True)
            now = self.now()
            state["admission_times_monotonic"] = [
                t for t in state["admission_times_monotonic"] if 0 <= now - t < self.window_seconds
            ]
            expired = len(state["penalty_expiry_monotonic"])
            state["penalty_expiry_monotonic"] = [
                t for t in state["penalty_expiry_monotonic"] if t > now
            ]
            expired -= len(state["penalty_expiry_monotonic"])
            if expired:
                self._event(state, "concurrency_restore", expired_penalties=expired)
            dead = [token for token, pid in state["active_tokens"].items() if not self._alive(pid)]
            for token in dead:
                del state["active_tokens"][token]
            if dead:
                self._event(
                    state,
                    "dead_local_worker_slots_released",
                    count=len(dead),
                    upstream_release_unknown=True,
                )
            try:
                yield state
            finally:
                if json.dumps(state, sort_keys=True) != before:
                    self._decorate(state)
                    temporary = self.directory / ("admission-state." + str(os.getpid()) + ".tmp")
                    temporary.write_text(json.dumps(state, separators=(",", ":")) + "\n")
                    temporary.replace(path)
                fcntl.flock(lock, fcntl.LOCK_UN)

    async def acquire(self):
        while True:
            with self._transaction() as state:
                now = self.now()
                last = state["last_sent_monotonic"]
                paced = last is None or now - last >= self.send_interval
                room = len(state["active_tokens"]) < max(
                    self.minimum, self.maximum - len(state["penalty_expiry_monotonic"])
                )
                if paced and room and len(state["admission_times_monotonic"]) < self.window_maximum:
                    state["physical_attempts"] += 1
                    token = str(state["physical_attempts"])
                    state["active_tokens"][token] = os.getpid()
                    state["admission_times_monotonic"].append(now)
                    state["last_sent_monotonic"] = now
                    self._event(state, "physical_request", attempt_id=token)
                    return token
            await self.sleep(0.1)

    def record(self, event, **values):
        with self._transaction() as state:
            self._event(state, event, **values)

    def penalize(self, reason, attempt_id):
        with self._transaction() as state:
            state["penalty_expiry_monotonic"].append(self.now() + self.penalty_seconds)
            self._event(state, "concurrency_reduce", reason=reason, attempt_id=attempt_id)

    def release(self, attempt_id):
        with self._transaction() as state:
            assert state["active_tokens"].get(str(attempt_id)) == os.getpid()
            del state["active_tokens"][str(attempt_id)]
            self._event(state, "physical_release", attempt_id=attempt_id)


class AttemptObservation:
    """One physical attempt, including streamed body, has at most one failure.

    The synchronous callback receives no text, headers or credentials. Observer
    errors are isolated from network results and slot release.
    """

    def __init__(self, gate, token):
        self.gate = gate
        self.token = token
        self.context = safe_diagnostic_context(gate)
        self.failed = False
        self.penalized = False
        self._notify("physical_request")

    def _notify(self, event, **values):
        callback = getattr(self.gate, "attempt_observer", None)
        if callback is None:
            return
        try:
            callback(
                {
                    "event": event,
                    "attempt_id": str(self.token),
                    "pid": os.getpid(),
                    "monotonic_seconds": self.gate.now(),
                    **self.context,
                    **values,
                }
            )
        except Exception as exc:
            # Monitoring must never change model behavior or leak a held slot.
            try:
                self.gate.record(
                    "attempt_observer_error", attempt_id=self.token, error_type=type(exc).__name__
                )
            except Exception:
                pass

    def http_failure(self, status):
        if status in (429, 504, 408) and not self.failed:
            self.failed = True
            self._notify(
                "physical_failure",
                http_status=status,
                error_type="HTTP" + str(status),
                is_timeout=False,
            )

    def timeout_failure(self, exc):
        if isinstance(exc, httpx.TimeoutException) and not self.failed:
            self.failed = True
            self._notify("physical_failure", error_type=type(exc).__name__, is_timeout=True)

    def penalize_once(self, reason):
        if not self.penalized:
            self.penalized = True
            self.gate.penalize(reason, self.token)


class ObservedStream(httpx.AsyncByteStream):
    def __init__(
        self,
        original,
        gate,
        token,
        started,
        client,
        observe_sse=True,
        observation=None,
        archive=None,
    ):
        self.original = original
        self.archive = archive
        self.json_buffer = bytearray() if not observe_sse else None
        self.gate = gate
        self.token = token
        self.started = started
        self.client = client
        self.observe_sse = observe_sse
        self.observation = observation
        self.closed = False
        self.error_recorded = False
        self.pending = b""
        self.last_byte = None
        self.last_progress = started
        self.metrics = {
            "first_byte_seconds": None,
            "first_sse_seconds": None,
            "first_content_seconds": None,
            "first_reasoning_seconds": None,
            "byte_chunks": 0,
            "bytes_received": 0,
            "sse_chunks": 0,
            "content_chunks": 0,
            "reasoning_chunks": 0,
            "content_characters": 0,
            "reasoning_characters": 0,
            "max_byte_gap_seconds": 0,
            "finish_reason": None,
            "done_seen": False,
            "json_parse_errors": 0,
            "usage": {},
            "stream_eof": False,
        }

    def record_error(self, exc):
        if isinstance(exc, GeneratorExit):
            return
        if not self.error_recorded:
            if self.archive is not None:
                self.archive.finish("ERROR", error_type=type(exc).__name__)
            self.error_recorded = True
            self.gate.record(
                "physical_error",
                attempt_id=self.token,
                error_type=type(exc).__name__,
                elapsed_seconds=self.gate.now() - self.started,
            )
            if isinstance(exc, httpx.TimeoutException):
                if self.observation is not None:
                    self.observation.timeout_failure(exc)
                    self.observation.penalize_once(type(exc).__name__)
                else:
                    self.gate.penalize(type(exc).__name__, self.token)

    def _line(self, line, now):
        if not line.startswith(b"data:"):
            return
        raw = line[5:].strip()
        if raw == b"[DONE]":
            self.metrics["done_seen"] = True
            return
        if not raw:
            return
        try:
            chunk = json.loads(raw)
        except (ValueError, UnicodeError):
            self.metrics["json_parse_errors"] += 1
            return
        if not isinstance(chunk, dict):
            return
        if self.archive is not None:
            self.archive.chunk(chunk)
        m = self.metrics
        m["sse_chunks"] += 1
        if m["first_sse_seconds"] is None:
            m["first_sse_seconds"] = now - self.started
        choices = chunk.get("choices") or []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                delta = {}
            for kind, field in [("content", "content"), ("reasoning", "reasoning_content")]:
                text = delta.get(field)
                if isinstance(text, str) and text:
                    m[kind + "_chunks"] += 1
                    m[kind + "_characters"] += len(text)
                    if m["first_" + kind + "_seconds"] is None:
                        m["first_" + kind + "_seconds"] = now - self.started
                        self.gate.record(
                            "stream_first_" + kind,
                            attempt_id=self.token,
                            elapsed_seconds=now - self.started,
                        )
            finish = choice.get("finish_reason")
            if finish is not None:
                m["finish_reason"] = (
                    finish
                    if finish in ("stop", "length", "tool_calls", "function_call", "content_filter")
                    else "other"
                )
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    m["usage"][key] = value
            detail = usage.get("completion_tokens_details")
            if isinstance(detail, dict) and isinstance(
                detail.get("reasoning_tokens"), (int, float)
            ):
                m["usage"]["reasoning_tokens"] = detail["reasoning_tokens"]

    def observe(self, data):
        now = self.gate.now()
        m = self.metrics
        m["byte_chunks"] += 1
        m["bytes_received"] += len(data)
        if m["first_byte_seconds"] is None:
            m["first_byte_seconds"] = now - self.started
            self.gate.record(
                "stream_first_byte", attempt_id=self.token, elapsed_seconds=now - self.started
            )
        if self.last_byte is not None:
            m["max_byte_gap_seconds"] = max(m["max_byte_gap_seconds"], now - self.last_byte)
        self.last_byte = now
        if self.json_buffer is not None:
            self.json_buffer.extend(data)
        if self.observe_sse:
            self.pending += data
            while b"\n" in self.pending:
                line, self.pending = self.pending.split(b"\n", 1)
                self._line(line.rstrip(b"\r"), now)
        if now - self.last_progress >= 30:
            self.gate.record(
                "stream_progress", attempt_id=self.token, elapsed_seconds=now - self.started, **m
            )
            self.last_progress = now

    async def __aiter__(self):
        try:
            async for data in self.original:
                self.observe(data)
                yield data
            if self.pending and self.observe_sse:
                self._line(self.pending.rstrip(b"\r"), self.gate.now())
            self.pending = b""
            self.metrics["stream_eof"] = True
            if self.json_buffer is not None and self.archive is not None:
                try:
                    payload = json.loads(self.json_buffer)
                except (ValueError, UnicodeError):
                    self.archive.finish("UNPARSEABLE_RESPONSE")
                else:
                    self.archive.chunk(payload, message=True)
                self.json_buffer.clear()
        except BaseException as exc:
            self.record_error(exc)
            raise
        finally:
            await self.aclose()

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        try:
            await self.original.aclose()
        except BaseException as exc:
            self.record_error(exc)
            raise
        finally:
            self.pending = b""
            if self.archive is not None:
                complete = (
                    self.metrics.get("done_seen")
                    if self.observe_sse
                    else self.metrics.get("stream_eof")
                )
                self.archive.finish("COMPLETE" if complete else "INTERRUPTED")
            self.gate.record(
                "stream_summary",
                attempt_id=self.token,
                elapsed_seconds=self.gate.now() - self.started,
                **self.metrics,
            )
            self.gate.release(self.token)
            self.client._bird_observed_streams.discard(self)


def _error_fields(payload, depth=0):
    """Flatten only error/code/type/message, including JSON-wrapped LiteLLM errors."""
    if depth > 5:
        return []
    if isinstance(payload, str):
        start, end = payload.find("{"), payload.rfind("}")
        if start >= 0 and end > start:
            try:
                return _error_fields(json.loads(payload[start : end + 1]), depth + 1)
            except ValueError:
                pass
        return [("message", payload)]
    if not isinstance(payload, dict):
        return []
    result = []
    for key in ("code", "type", "message", "error"):
        value = payload.get(key)
        if isinstance(value, (dict, str)):
            if key in ("error", "message"):
                result.extend(_error_fields(value, depth + 1))
            elif isinstance(value, str):
                result.append((key, value))
    return result


def _safe_error_text(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)sk-[a-z0-9_.-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"\']+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b(?:hashed[_ -]?(?:api[_ -]?)?key|(?:api[_ -]?)?key[_ -]?(?:id|identifier|hash)|api[_ -]?key|key|token|password|authorization)\b[\"\']?\s*[:=]\s*(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;}]+)",
        "[REDACTED_FIELD]",
        text,
    )
    text = re.sub(r"(?i)\b[a-f0-9]{16,}\b", "[REDACTED_HASH]", text)
    # Error diagnostics never need echoed model inputs or other raw metadata.
    text = re.sub(
        r"(?is)\b(?:prompt|messages|request_body|metadata)\b[\"\']?\s*[:=].*",
        "[REDACTED_INPUT]",
        text,
    )
    return text[:2000]


def diagnostics_429(payload, headers, secrets=()):
    diagnostics = {}
    for key, value in _error_fields(payload):
        clean = _safe_error_text(value, secrets)
        diagnostics[key] = (
            diagnostics.get(key, "") + ("; " if key in diagnostics else "") + clean
        )[:2000]
    safe_headers = {}
    allowed = (
        "retry-after",
        "retry-after-ms",
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    )
    for name in allowed:
        value = headers.get(name, "")
        if re.fullmatch(r"\d{1,12}(?:\.\d{1,6})?(?:ms|s|m|h)?", value):
            safe_headers[name] = value
    diagnostics["headers"] = safe_headers
    return diagnostics


def classify_429(payload):
    """Return only an enum and numeric limits; never retain server text.

    Inspect the documented JSON error fields, not arbitrary metadata, request
    echoes, headers or key identifiers. Ambiguous responses remain unknown.
    """
    text = " ".join(value for _, value in _error_fields(payload)).lower()
    normalized = re.sub(r"[_-]+", " ", text)
    concurrent = bool(
        re.search(r"\bconcurren(?:cy|t)\b|\b(?:max )?parallel requests?\b|并发", normalized)
    )
    window = bool(
        re.search(
            r"\bwindow\b|\brpm\b|requests? per minute|requests? per \d+|窗口|每分钟", normalized
        )
    )
    kind = (
        "concurrency"
        if concurrent and not window
        else "window"
        if window and not concurrent
        else "unknown"
    )
    limits = {}
    # Values are emitted only with explicit limit labels; never copy arbitrary
    # numbers from server text (which can include credentials/identifiers).
    for name, pattern in {
        "concurrency_limit": r"(?:concurrency|concurrent|max parallel requests|parallel requests?|并发)(?:\s+(?:requests?|limit|max(?:imum)?))*\s*[:=]\s*(\d{1,7})(?![\w.])",
        "requests_per_minute": r"\brpm\s*[:=]\s*(\d{1,7})(?![\w.])",
        "window_seconds": r"\bwindow(?:\s+seconds)?\s*[:=]\s*(\d{1,7})(?![\w.])",
    }.items():
        match = re.search(pattern, normalized)
        if match:
            limits[name] = int(match.group(1))
    return kind, limits


def safe_diagnostic_context(gate):
    callback = getattr(gate, "diagnostic_context", None)
    if callback is None:
        return {}
    try:
        value = callback()
    except Exception:
        return {}
    if not isinstance(value, dict):
        return {}
    result = {}
    case_id = value.get("case_id")
    if isinstance(case_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}__q[0-9]{1,10}", case_id):
        result["case_id"] = case_id
    call_id = value.get("logical_call_id")
    if type(call_id) is int and 0 < call_id < 2**63:
        result["logical_call_id"] = call_id
    return result


def actual_request_configuration(request, secrets=()):
    """Extract only model settings from an already-buffered serialized request."""
    payload = json.loads(request.content)
    if not isinstance(payload, dict):
        raise ValueError("Model request JSON must be an object")
    model = payload.get("model")
    if (
        not isinstance(model, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", model)
        or _safe_error_text(model, secrets) != model
    ):
        model = None
    thinking = payload.get("thinking")
    thinking_type = thinking.get("type") if isinstance(thinking, dict) else None
    if thinking_type not in ("enabled", "disabled", "auto"):
        thinking_type = None
    return {
        "model": model,
        "thinking": {"type": thinking_type},
        "max_tokens": payload.get("max_tokens") if type(payload.get("max_tokens")) is int else None,
        "max_completion_tokens": payload.get("max_completion_tokens")
        if type(payload.get("max_completion_tokens")) is int
        else None,
        "reasoning_effort_present": "reasoning_effort" in payload,
    }


def wrap_send(original_send, get_gate, timeout_type, is_target):
    async def controlled_send(client, request, *args, **kwargs):
        if not is_target(request):
            return await original_send(client, request, *args, **kwargs)
        gate = get_gate()
        token = await gate.acquire()
        started = gate.now()
        observation = AttemptObservation(gate, token)
        archive = begin_archive(gate, token, observation.context)
        delegated = False
        try:
            try:
                gate.record(
                    "actual_request_configuration",
                    attempt_id=token,
                    **observation.context,
                    **actual_request_configuration(
                        request, getattr(gate, "diagnostic_secrets", ())
                    ),
                )
            except Exception as exc:
                # Auditing must not change the request, retry, penalty or release behavior.
                try:
                    gate.record(
                        "actual_request_configuration_error",
                        attempt_id=token,
                        **observation.context,
                        error_type=type(exc).__name__,
                    )
                except Exception:
                    pass
            response = await original_send(client, request, *args, **kwargs)
            gate.record(
                "physical_response",
                attempt_id=token,
                http_status=response.status_code,
                elapsed_seconds=gate.now() - started,
            )
            observation.http_failure(response.status_code)
            if response.status_code >= 400:
                archive.finish("HTTP_ERROR", http_status=response.status_code)
            if response.status_code in (408, 504):
                observation.penalize_once("HTTP" + str(response.status_code))
            if response.status_code == 429:
                # HTTPX caches aread() as response.content, so the SDK can still
                # consume the exact body and apply its configured retry policy.
                await response.aread()
                try:
                    payload = response.json()
                except (ValueError, UnicodeError):
                    payload = None
                classification, limits = classify_429(payload)
                gate.record(
                    "http429_classified",
                    attempt_id=token,
                    classification=classification,
                    limits=limits,
                    **safe_diagnostic_context(gate),
                    diagnostics=diagnostics_429(
                        payload, response.headers, getattr(gate, "diagnostic_secrets", ())
                    ),
                )
                if classification == "concurrency":
                    observation.penalize_once("HTTP429_concurrency")
            if (
                kwargs.get("stream", False)
                and response.status_code != 429
                and not response.is_stream_consumed
            ):
                if not hasattr(client, "_bird_observed_streams"):
                    client._bird_observed_streams = set()
                wrapped = ObservedStream(
                    response.stream,
                    gate,
                    token,
                    started,
                    client,
                    "text/event-stream" in response.headers.get("content-type", ""),
                    observation=observation,
                    archive=archive,
                )
                client._bird_observed_streams.add(wrapped)
                response.stream = wrapped
                delegated = True
                gate.record(
                    "stream_headers",
                    attempt_id=token,
                    http_status=response.status_code,
                    elapsed_seconds=gate.now() - started,
                )
            if not delegated and response.status_code < 400:
                try:
                    archive.chunk(response.json(), message=True)
                except (ValueError, UnicodeError):
                    archive.finish("UNPARSEABLE_RESPONSE")
                else:
                    archive.finish("COMPLETE", http_status=response.status_code)
            return response
        except BaseException as exc:
            archive.finish("ERROR", error_type=type(exc).__name__)
            if not isinstance(exc, GeneratorExit):
                gate.record(
                    "physical_error",
                    attempt_id=token,
                    error_type=type(exc).__name__,
                    elapsed_seconds=gate.now() - started,
                )
            if isinstance(exc, timeout_type):
                observation.timeout_failure(exc)
                observation.penalize_once(type(exc).__name__)
            raise
        finally:
            if not delegated:
                gate.release(token)

    return controlled_send


def wrap_close(original_close):
    async def controlled_close(client):
        try:
            for stream in list(getattr(client, "_bird_observed_streams", ())):
                await stream.aclose()
        finally:
            await original_close(client)

    return controlled_close


def install_gate(
    directory, base_url, *, diagnostic_secrets=(), diagnostic_context=None, attempt_observer=None
):
    from urllib.parse import urlsplit

    import httpx

    target = urlsplit(base_url.rstrip("/") + "/chat/completions")

    def matches(request):
        return (
            request.method == "POST"
            and request.url.host == target.hostname
            and (request.url.port or (443 if request.url.scheme == "https" else 80))
            == (target.port or (443 if target.scheme == "https" else 80))
            and request.url.path.rstrip("/") == target.path
        )

    gate = AdmissionGate(directory)
    gate.diagnostic_secrets = tuple(diagnostic_secrets)
    gate.diagnostic_context = diagnostic_context
    gate.attempt_observer = attempt_observer
    httpx.AsyncClient.send = wrap_send(
        httpx.AsyncClient.send, lambda: gate, httpx.TimeoutException, matches
    )
    httpx.AsyncClient.aclose = wrap_close(httpx.AsyncClient.aclose)
    return gate
