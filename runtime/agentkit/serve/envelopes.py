from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def success(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"success": True, "data": data}, status_code=status_code)


def error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "RESOURCE_NOT_FOUND",
    409: "CONFLICT",
    415: "UNSUPPORTED_FILE_TYPE",
    422: "BAD_REQUEST",
    429: "RATE_LIMITED",
}


def error_code(status_code: int) -> str:
    """Envelope code for a bare HTTP status."""
    if status_code >= 500:
        return "INTERNAL_ERROR"
    return _STATUS_CODES.get(status_code, "BAD_REQUEST")


def register_error_handlers(app: FastAPI) -> None:
    """Render HTTPException and request validation errors as the error envelope."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        response = error(error_code(exc.status_code), str(exc.detail), exc.status_code)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = first.get("msg", "Invalid request")
        return error("BAD_REQUEST", f"{location}: {message}" if location else message, 422)
