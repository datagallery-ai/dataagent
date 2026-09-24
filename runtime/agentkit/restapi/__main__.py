"""Single backend entrypoint for the console command and python -m restapi."""

import argparse
import json
import logging
import os
import socket
import sys
import threading
from logging.handlers import RotatingFileHandler

from dataagent import LaunchOptions, Runtime, prepare_runtime, safe_error


def check_port(host: str, port: int):
    with socket.socket() as probe:
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
            probe.listen(1)
        except OSError:
            raise RuntimeError(f"Port {host}:{port} is occupied; no existing process was changed") from None


def configure_logging(runtime: Runtime):
    for path in (runtime.paths.state_dir, runtime.paths.state_dir / "logs"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (runtime.paths.state_dir, runtime.paths.state_dir / "logs"):
        path.chmod(0o700)
    directory = runtime.paths.state_dir / "logs"
    secrets = runtime.redaction_secrets

    class SafeFormatter(logging.Formatter):
        def format(self, record):
            return safe_error(RuntimeError(super().format(record)), secrets)["message"]

    handler = RotatingFileHandler(directory / "backend.log", maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("dataagent.startup").info("Enabled plugins and sources: %s",
                                              runtime.report.plugin_origins)


def serve(runtime: Runtime, *, stdio_ready=False):
    import uvicorn

    from restapi.app import create_app

    if stdio_ready and not os.environ.get("DATAAGENT_V2_INSTANCE_ID"):
        raise ValueError("A launch instance identity is required for the startup protocol")
    settings = runtime.settings.server
    check_port(settings.host, settings.port)
    configure_logging(runtime)
    app = create_app(runtime, instance_id=os.environ.get("DATAAGENT_V2_INSTANCE_ID"))
    server = uvicorn.Server(uvicorn.Config(
        app, host=settings.host, port=settings.port,
        log_config=None, access_log=False, timeout_graceful_shutdown=5,
    ))
    stopped = threading.Event()

    def announce_ready():
        while not stopped.wait(0.05):
            if server.started:
                print(json.dumps({
                    "type": "ready",
                    "protocol": "dataagent-v2", "instanceId": os.environ.get("DATAAGENT_V2_INSTANCE_ID"),
                    "runtimeUrl": f"http://{settings.host}:{settings.port}/dataagent/stream",
                }), flush=True)
                return

    def watch_frontend():
        # The TUI owns the pipe's write end, including when uv is the immediate
        # parent. EOF detects frontend death without relying on PID ancestry.
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dataagent", description="DataAgent backend")
    commands = parser.add_subparsers(dest="command", required=True)
    api = commands.add_parser("serve", help="Run the local AG-UI backend without a frontend")
    api.add_argument("--config", help="Additional configuration above Home defaults")
    api.add_argument("--user", default="default", help="Local profile id (default: default)")
    api.add_argument("--workspace", action="append", default=None,
                     help="Read-only input workspace; repeat for more than one")
    api.add_argument("--session", help="Resume a session that belongs to this profile")
    api.add_argument("--env-file", help="Environment file resolved by the backend")
    api.add_argument("--stdio-ready", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    secrets = ()
    try:
        options = LaunchOptions(
            config=args.config, workspaces=tuple(args.workspace or ()), user_id=args.user,
            session_id=args.session, env_file=args.env_file, init_home=True,
        )
        runtime = prepare_runtime(options)
        secrets += runtime.redaction_secrets
        return serve(runtime, stdio_ready=args.stdio_ready)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"DataAgent: {safe_error(error, secrets)['message']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
