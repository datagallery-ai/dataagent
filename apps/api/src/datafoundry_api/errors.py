from __future__ import annotations


class ApiError(Exception):
    """Stable HTTP-facing error returned through the API envelope."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class AuthError(ApiError):
    """Authentication or authorization failure."""


class ResourceError(ApiError):
    """Workspace, Skill, or MCP resource failure."""
