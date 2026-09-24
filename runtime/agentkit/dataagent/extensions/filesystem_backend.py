"""Assemble native file and shell access with a session-specific working directory.

Both use host permissions without sandboxing. Input protection and output placement
are prompt conventions, not enforced access boundaries.
"""

from deepagents.backends import CompositeBackend, LocalShellBackend

from dataagent.bootstrap import Runtime
from dataagent.bootstrap.paths import SessionPaths, ensure_session


def build_filesystem_backend(runtime: Runtime, session: SessionPaths) -> CompositeBackend:
    """Use native operations, keeping the session cwd and artifact location explicit."""
    ensure_session(runtime.paths.home, session)
    backend = LocalShellBackend(
        root_dir=session.outputs,
        virtual_mode=False,
        timeout=runtime.timeout_seconds,
        inherit_env=True,
    )
    return CompositeBackend(default=backend, routes={}, artifacts_root=str(session.artifacts))
