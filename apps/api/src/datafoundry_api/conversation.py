"""Mount agentkit conversation routes on the control-plane process.

The handlers stay in ``restapi``. This module only prepares that runtime and
opens the same lifespan the standalone ``dataagent serve`` command uses.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

logger = logging.getLogger("datafoundry_api.conversation")


def is_conversation_path(path: str) -> bool:
    """True for agentkit routes mounted on this process."""
    if path in {"/dataagent/stream", "/api/copilotkit", "/sessions"}:
        return True
    return path.startswith(("/dataagent/stream/", "/api/copilotkit/", "/sessions/"))


class ConversationMount:
    """In-process agentkit runtime, registered onto the control-plane app."""

    def __init__(self) -> None:
        self.runtime = prepare_conversation_runtime()
        self.instance_id = (os.environ.get("DATAAGENT_V2_INSTANCE_ID") or "").strip() or str(uuid4())

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        app.state.conversation_runtime = self.runtime
        app.state.conversation_instance_id = self.instance_id
        async with open_conversation(app, self.runtime):
            yield

    def register(self, app: FastAPI) -> None:
        if self.runtime is None:
            return
        from restapi.app import register_conversation_routes

        register_conversation_routes(
            app,
            self.runtime,
            instance_id=self.instance_id,
            include_health=False,
        )


def prepare_conversation_runtime():
    """Build the agentkit runtime. A setup failure leaves the control plane up."""
    try:
        from dataagent import LaunchOptions, prepare_runtime

        config = _conversation_config()
        runtime = prepare_runtime(
            LaunchOptions(
                config=config,
                init_home=True,
                cwd=config.parent if config is not None else Path.cwd(),
            )
        )
    except Exception as exc:  # noqa: BLE001 - a setup failure must not take down the control plane
        logger.warning("Conversation engine is not ready: %s", exc)
        return None
    return runtime


@asynccontextmanager
async def open_conversation(app: FastAPI, runtime):
    """Run agentkit's lifespan. A startup failure leaves the control plane up."""
    if runtime is None:
        yield
        return
    from restapi.app import conversation_lifespan

    started = False
    try:
        async with conversation_lifespan(app, runtime):
            started = True
            yield
    except Exception as exc:
        if started:
            raise
        logger.warning("Conversation engine failed during startup: %s", exc)
        app.state.agents = None
        yield


def conversation_status(app: FastAPI) -> dict:
    """Report whether the in-process engine is ready."""
    runtime = getattr(getattr(app.state, "agents", None), "runtime", None)
    if runtime is None:
        return {"ready": False}
    from restapi.app import agentkit_health_body

    instance_id = getattr(app.state, "conversation_instance_id", "")
    return {"ready": True, **agentkit_health_body(runtime, instance_id)}


def _conversation_config() -> Path | None:
    override = (os.environ.get("DATAAGENT_CONFIG") or "").strip()
    if override:
        return Path(override).expanduser()
    return None
