import json
import logging
import os
import subprocess
import sys

import pytest

from dataagent import LaunchOptions, SemanticServiceClient, prepare_runtime
from dataagent.diagnostics.logging import configure_logging, log_context, register_log_secrets
from dataagent.diagnostics.render import log_event, redact_log_text, safe_error


@pytest.fixture
def configured_logging(runtime, monkeypatch):
    monkeypatch.setattr("dataagent.diagnostics.logging._secrets", set())
    root = logging.getLogger()
    original = root.handlers[:]
    level = root.level
    configure_logging(runtime)
    yield runtime.paths.state_dir / "logs" / "backend.log"
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.handlers = original
    root.setLevel(level)


def test_full_diagnostics_and_hot_reload_secrets(configured_logging):
    logger = logging.getLogger("plugin.example")
    register_log_secrets(("old-key",))
    register_log_secrets(("new-key",))
    token = log_context.set({"threadId": "thread-test", "runId": "run-test"})
    try:
        try:
            raise ValueError("old-key new-key " + "x" * 4000 + "\nROOT_CAUSE_END")
        except ValueError as error:
            logger.exception("plugin failed")
            assert len(safe_error(error)["message"]) <= 1500
    finally:
        log_context.reset(token)
    text = configured_logging.read_text()
    record = json.loads(text.strip().splitlines()[-1])
    assert record["component"] == "plugin.example"
    assert record["runId"] == "run-test"
    assert "test_full_diagnostics_and_hot_reload_secrets" in record["stack"]
    assert "\nROOT_CAUSE_END" in record["stack"]
    assert "old-key" not in text and "new-key" not in text


def test_signed_urls_and_otlp_auth_are_redacted(configured_logging):
    logging.getLogger("plugin").error(
        "GET https://user:pass@example.invalid/log?X-Amz-Signature=private-signature "
        "Authorization: Bearer private-bearer"
    )
    text = configured_logging.read_text()
    assert "private-signature" not in text and "private-bearer" not in text
    assert "user:pass" not in text


def test_independent_backend_startup_failure_is_persisted(tmp_path):
    home = tmp_path / "standalone"
    config = tmp_path / "bad.yaml"
    config.write_text("{invalid json")
    result = subprocess.run(
        [sys.executable, "-m", "dataagent", "serve", "--config", str(config)],
        env={**os.environ, "DATAAGENT_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    records = [
        json.loads(line) for line in (home / "runtime/logs/backend.log").read_text().splitlines()
    ]
    assert any(record.get("stage") == "prepare" and record.get("stack") for record in records)
    assert not list(home.rglob("deploy-*.log"))


def test_rotated_backend_files_remain_private(configured_logging):
    from dataagent.diagnostics.logging import PrivateRotatingFileHandler

    handler = next(
        h for h in logging.getLogger().handlers if isinstance(h, PrivateRotatingFileHandler)
    )
    handler.maxBytes = 200
    for _ in range(8):
        logging.getLogger("plugin").warning("x" * 300)
    files = list(configured_logging.parent.glob("backend.log*"))
    assert len(files) == 4
    if os.name != "nt":
        assert all(file.stat().st_mode & 0o777 == 0o600 for file in files)


def test_large_diagnostic_redaction_is_bounded():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dataagent.diagnostics.render import redact_log_text; "
            "value = redact_log_text('x' * 500000 + '\\nBearer large-log-secret'); "
            "assert len(value) >= 500000 and 'large-log-secret' not in value",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_quoted_credentials_with_spaces_are_fully_redacted(configured_logging):
    logging.getLogger("plugin").error(
        '{"password":"first middle last"} {"cookie": "session=first; id=last"}'
    )
    text = configured_logging.read_text()
    assert "first" not in text and "last" not in text


def test_runtime_registers_credentials_without_scrubbing_addresses_and_parameters(
    configured_logging, tmp_path, monkeypatch
):
    monkeypatch.setenv("KEYBOARD_LAYOUT", "en")
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "false")
    home = tmp_path / "home"
    config = home / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "fixture",
                        "base_url": "http://model.invalid/v1",
                        "api_key": "model-private-key",
                    }
                },
                "semantic": {
                    "base_url": "http://10.0.0.8:8080",
                    "username": "svc",
                    "password": "semantic-private-password",
                },
                "plugins": {"enabled": []},
            }
        )
    )
    (home / ".mcp.yaml").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "review": {
                        "command": "node",
                        "args": [
                            "npx",
                            "--token",
                            "cli-private-token",
                            "--password=cli-private-password",
                        ],
                        "env": {"SCENARIO": "svc", "LANG": "en", "API_KEY": "mcp-private-key"},
                    }
                }
            }
        )
    )
    prepared = prepare_runtime(LaunchOptions(cwd=tmp_path, config=config))
    ordinary = ("svc", "http://10.0.0.8:8080", "npx", "en")
    assert all(value not in prepared.redaction_secrets for value in ordinary)
    credentials = (
        "model-private-key",
        "semantic-private-password",
        "mcp-private-key",
        "cli-private-token",
        "cli-private-password",
    )
    assert all(value in prepared.redaction_secrets for value in credentials)
    configure_logging(prepared)
    try:
        raise ConnectionError(
            "target=http://10.0.0.8:8080 path=/opt/svc/npx/client.py open false "
            + " ".join(credentials)
        )
    except ConnectionError:
        logging.getLogger("svc.worker").exception("connection failed")
    record = json.loads(configured_logging.read_text().splitlines()[-1])
    assert record["component"] == "svc.worker"
    assert "target=http://10.0.0.8:8080 path=/opt/svc/npx/client.py open false" in record["stack"]
    assert all(value not in json.dumps(record) for value in credentials)


def test_short_credentials_redact_complete_values_without_corrupting_words_or_paths():
    assert redact_log_text("open /opt/en/client.py supplied en password=en", ("en",)) == (
        "open /opt/en/client.py supplied [REDACTED] password=[REDACTED]"
    )
    assert redact_log_text("https://host/rpc?token_count=128&key=abc") == (
        "https://host/rpc?token_count=128&key=[REDACTED]"
    )
    assert redact_log_text("https://user-only-token@host/path") == "https://[REDACTED]@host/path"
    assert redact_log_text("denied \x1b[31men\x1b[0m", ("en",)) == "denied [REDACTED]"
    assert redact_log_text(r"open C:\opt\en\client.py en.config en/client.py", ("en",)) == (
        r"open C:\opt\en\client.py en.config en/client.py"
    )


@pytest.mark.parametrize("suffix", [".", ":", ": denied", "...", ";", "!"])
def test_short_credentials_before_punctuation_are_redacted_in_public_errors_and_logs(suffix):
    message = f"Incorrect API key provided: abc123{suffix}"
    expected = f"Incorrect API key provided: [REDACTED]{suffix}"
    assert safe_error(RuntimeError(message), ("abc123",))["message"] == expected
    assert redact_log_text(message, ("abc123",)) == expected
    assert (
        redact_log_text(f"provided:abc123{suffix}", ("abc123",)) == f"provided:[REDACTED]{suffix}"
    )


def test_history_sql_keeps_working_when_the_log_directory_cannot_be_created(tmp_path):
    home = tmp_path / "home"
    (home / "runtime").mkdir(parents=True)
    (home / "runtime/logs").write_text("not a directory")
    (home / "config.yaml").write_text(
        json.dumps(
            {
                "models": {
                    "primary": {
                        "name": "fixture",
                        "base_url": "http://model.invalid/v1",
                        "api_key": "fixture-api-key",
                    }
                },
                "plugins": {"enabled": []},
            }
        )
    )
    result = subprocess.run(
        [sys.executable, "-m", "dataagent", "history-sql", "--if-enabled"],
        env={**os.environ, "DATAAGENT_HOME": str(home), "DATAAGENT_LOG_CAPTURE": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "disabled"
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert any(record.get("stage") == "logging" for record in records)


def test_borrowed_semantic_credentials_do_not_register_the_username():
    with SemanticServiceClient("http://10.0.0.8:8080", auth=("svc", "private-password")) as client:
        assert "svc" not in client.redaction_secrets
        assert "private-password" in client.redaction_secrets


def test_a2a_events_use_the_shared_json_writer_and_preserve_structured_fields(configured_logging):
    log_event(
        logging.getLogger("dataagent.a2a"),
        "a2a.assembly.failed",
        secrets=(),
        request_id="request-review",
        root_run_id="run-review",
        duration_ms=12,
        frames=[{"file": "/opt/svc/client.py", "line": 7}],
        message="failed test-secret",
    )
    record = json.loads(configured_logging.read_text().splitlines()[-1])
    assert record["event"] == "a2a.assembly.failed"
    assert record["request_id"] == "request-review"
    assert record["root_run_id"] == "run-review"
    assert record["duration_ms"] == 12
    assert record["frames"] == [{"file": "/opt/svc/client.py", "line": 7}]
    assert record["message"] == "failed [REDACTED]"
