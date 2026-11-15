"""Single backend entrypoint for the console command and python -m serve."""

import argparse
import json
import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

# Must run before LangSmith Client import/use: avoid `git describe` blocking
# the event loop (no model calls, /healthz hangs).
os.environ.setdefault("LANGCHAIN_REVISION_ID", "local")

from dataagent import (
    ConfigurationLoadError,
    LaunchOptions,
    Runtime,
    prepare_runtime,
    safe_error,
)
from dataagent.host import server_listen

# Reload watches source only. The project root also holds .venv, which the
# polling reloader would rescan every tick.
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def http_origin(host: str, port: int) -> str:
    """HTTP origin for a bind address. IPv6 literals are bracketed so the URL parses."""
    bare = host.strip()
    if bare.startswith("[") and bare.endswith("]"):
        bare = bare[1:-1].strip()
    shown = f"[{bare}]" if ":" in bare else bare
    return f"http://{shown}:{port}"


def runtime_url(host: str, port: int) -> str:
    """Ready-message URL. The client rewrites wildcard binds; this only keeps the URL legal."""
    return f"{http_origin(host, port)}/dataagent/stream"


def check_port(host: str, port: int):
    with socket.socket() as probe:
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
            probe.listen(1)
        except OSError:
            raise RuntimeError(
                f"Port {host}:{port} is occupied; no existing process was changed"
            ) from None


def configure_logging(runtime: Runtime | None):
    handlers = []
    plain = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    if runtime is None:
        stderr = logging.StreamHandler(sys.stderr)
        stderr.setFormatter(plain)
        handlers.append(stderr)
    else:
        for path in (runtime.paths.state_dir, runtime.paths.state_dir / "logs"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (runtime.paths.state_dir, runtime.paths.state_dir / "logs"):
            path.chmod(0o700)
        secrets = runtime.redaction_secrets

        class SafeFormatter(logging.Formatter):
            def format(self, record):
                return safe_error(RuntimeError(super().format(record)), secrets)["message"]

        file_handler = RotatingFileHandler(
            runtime.paths.state_dir / "logs" / "backend.log",
            maxBytes=5_000_000,
            backupCount=3,
        )
        file_handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        stderr = logging.StreamHandler(sys.stderr)
        stderr.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handlers.extend((file_handler, stderr))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    if runtime is not None:
        logging.getLogger("dataagent.startup").info(
            "Enabled plugins and sources: %s", runtime.report.plugin_origins,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dataagent", description="DataAgent backend")
    commands = parser.add_subparsers(dest="command", required=True)
    api = commands.add_parser("serve", help="Run the product backend")
    api.add_argument(
        "--config",
        help="Use this file instead of Home config.json",
    )
    api.add_argument("--user", default="default", help="Local profile id (default: default)")
    api.add_argument(
        "--workspace", action="append", default=None,
        help="Read-only input workspace; repeat for more than one",
    )
    api.add_argument(
        "--session",
        help="Adopt that session's recorded workspace binding as the process default",
    )
    api.add_argument("--env-file", help="Environment file resolved by the backend")
    api.add_argument(
        "--reload",
        action="store_true",
        help="Restart when serve, dataagent, or a2a_server source changes",
    )
    # Hidden handshake used when the TUI launches this backend.
    api.add_argument("--stdio-ready", action="store_true", help=argparse.SUPPRESS)
    a2a = commands.add_parser("serve-a2a", help="Expose this agent over the A2A 1.0 protocol")
    a2a.add_argument(
        "--config",
        help="Use this file instead of Home config.json",
    )
    a2a.add_argument("--user", default="default", help="Local profile id (default: default)")
    a2a.add_argument(
        "--workspace", action="append", default=None,
        help="Read-only input workspace; repeat for more than one",
    )
    a2a.add_argument("--env-file", help="Environment file resolved by the backend")
    a2a.add_argument("--host", default=None, help="Bind address (default: config server.host)")
    a2a.add_argument(
        "--port", "-p", type=int, default=None, help="Bind port (default: config server.port)",
    )
    a2a.add_argument(
        "--auth-token", default=None,
        help="Require Authorization: Bearer <token> (default: DATAAGENT_A2A_AUTH_TOKEN)",
    )
    return parser


def _prepare(args):
    try:
        return prepare_runtime(LaunchOptions(
            config=args.config or None,
            workspaces=tuple(args.workspace or ()),
            user_id=args.user,
            session_id=getattr(args, "session", None),
            env_file=args.env_file,
            init_home=True,
        ))
    except ConfigurationLoadError as error:
        print(f"DataAgent: {safe_error(error, ())['message']}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    except Exception as error:
        print(f"DataAgent: {safe_error(error, ())['message']}", file=sys.stderr, flush=True)
        return None


def _instance_id() -> str:
    return (os.environ.get("DATAAGENT_INSTANCE_ID") or "").strip() or str(uuid4())


def _listen(
    runtime: Runtime | None, config: str | None = None, env_file: str | None = None
) -> tuple[str, int]:
    """Bind only config.json server.host and server.port, also in a degraded start."""
    if runtime is not None:
        return runtime.settings.server.host, runtime.settings.server.port
    return server_listen(LaunchOptions(config=config or None, env_file=env_file or None))


def reload_app():
    """Uvicorn factory for ``dataagent serve --reload``. Rebuilds the app from DATAAGENT_SERVE_ARGV."""
    raw = os.environ.get("DATAAGENT_SERVE_ARGV", "")
    if not raw:
        raise RuntimeError("DATAAGENT_SERVE_ARGV is required to reload the backend")
    from serve.app import create_app

    args = _parser().parse_args(json.loads(raw))
    runtime = _prepare(args)
    configure_logging(runtime)
    return create_app(runtime, instance_id=_instance_id())


def serve(runtime: Runtime | None, host: str, port: int, *, stdio_ready: bool = False) -> int:
    import uvicorn

    from serve.app import create_app

    if stdio_ready and not os.environ.get("DATAAGENT_INSTANCE_ID"):
        raise ValueError("A launch instance identity is required for the startup protocol")
    check_port(host, port)
    configure_logging(runtime)
    app = create_app(runtime, instance_id=_instance_id())
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port,
        log_config=None, access_log=False, timeout_graceful_shutdown=5,
    ))
    stopped = threading.Event()

    def announce_ready():
        while not stopped.wait(0.05):
            if server.started:
                print(json.dumps({
                    "type": "ready",
                    "protocol": "dataagent",
                    "instanceId": os.environ.get("DATAAGENT_INSTANCE_ID"),
                    "runtimeUrl": runtime_url(host, port),
                }), flush=True)
                return

    def watch_frontend():
        try:
            while sys.stdin.buffer.read(1):
                pass
        finally:
            if not stopped.is_set():
                server.should_exit = True

    if stdio_ready:
        threading.Thread(target=announce_ready, daemon=True).start()
        threading.Thread(target=watch_frontend, daemon=True).start()
    try:
        server.run()
    finally:
        stopped.set()
    return 0 if server.started else 1


def serve_a2a(runtime: Runtime, args) -> int:
    """Expose this runtime over A2A. Holds the same workspace lock as ``serve``."""
    from a2a_server import run_a2a_server
    from a2a_server.agent_card import JSONRPC_PATH

    host = args.host or runtime.settings.server.host
    port = args.port or runtime.settings.server.port
    check_port(host, port)
    configure_logging(runtime)
    logging.getLogger("dataagent.a2a").info("A2A JSON-RPC %s%s", http_origin(host, port), JSONRPC_PATH)
    run_a2a_server(
        runtime, host=host, port=port, auth_token=args.auth_token or runtime.a2a_auth_token,
    )
    return 0


def _run_reload(argv, host: str, port: int) -> int:
    payload = list(argv) if argv is not None else sys.argv[1:]
    os.environ["DATAAGENT_SERVE_ARGV"] = json.dumps(payload)
    try:
        check_port(host, port)
    except Exception as error:
        print(f"DataAgent: {safe_error(error, ())['message']}", file=sys.stderr)
        return 1
    import uvicorn

    uvicorn.run(
        "serve.__main__:reload_app",
        factory=True,
        host=host,
        port=port,
        reload=True,
        reload_dirs=[
            str(SOURCE_ROOT / "serve"),
            str(SOURCE_ROOT / "dataagent"),
            str(SOURCE_ROOT / "a2a_server"),
        ],
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    runtime = None
    try:
        runtime = _prepare(args)
        if args.command == "serve-a2a":
            if runtime is None:
                return 1
            return serve_a2a(runtime, args)
        if args.stdio_ready and runtime is None:
            return 1
        host, port = _listen(runtime, args.config, args.env_file)
        if args.reload:
            return _run_reload(argv, host, port)
        return serve(runtime, host, port, stdio_ready=args.stdio_ready)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        secrets = runtime.redaction_secrets if runtime is not None else ()
        print(f"DataAgent: {safe_error(error, secrets)['message']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
