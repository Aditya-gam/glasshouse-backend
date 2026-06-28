"""RFC 9457 problem+json error model (error-model.md).

Typed domain exceptions are transport-agnostic; one set of handlers maps them — and FastAPI's
HTTPException / validation errors — to ``application/problem+json``. Never leak stack traces or
internals. Fail closed: ownership failures surface as 404 (no IDOR existence signal).
"""

from typing import Any, cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_PROBLEM_BASE = "https://glasshouse.app/problems"
_MEDIA_TYPE = "application/problem+json"


class DomainError(Exception):
    """Base for transport-agnostic domain failures; mapped to problem+json at the edge."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    problem_type: str = "about:blank"
    title: str = "Request failed"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__(detail or self.title)


class ConsentMissing(DomainError):
    status_code = status.HTTP_403_FORBIDDEN
    problem_type = f"{_PROBLEM_BASE}/consent-missing"
    title = "Consent required"


class NotFound(DomainError):
    """Also used for ownership failures — hide existence rather than signal an IDOR target."""

    status_code = status.HTTP_404_NOT_FOUND
    problem_type = f"{_PROBLEM_BASE}/not-found"
    title = "Not found"


class NotImplementedYet(DomainError):
    """A contract-first stub: the endpoint's engine milestone has not landed yet."""

    status_code = status.HTTP_501_NOT_IMPLEMENTED
    problem_type = f"{_PROBLEM_BASE}/not-implemented"
    title = "Not implemented yet"


def _problem(
    *, status_code: int, title: str, problem_type: str, detail: str | None, instance: str
) -> JSONResponse:
    body: dict[str, Any] = {"type": problem_type, "title": title, "status": status_code}
    if detail:
        body["detail"] = detail
    body["instance"] = instance
    return JSONResponse(status_code=status_code, content=body, media_type=_MEDIA_TYPE)


async def _domain_handler(request: Request, exc: Exception) -> JSONResponse:
    err = cast(DomainError, exc)
    return _problem(
        status_code=err.status_code,
        title=err.title,
        problem_type=err.problem_type,
        detail=err.detail,
        instance=request.url.path,
    )


async def _http_handler(request: Request, exc: Exception) -> JSONResponse:
    err = cast(StarletteHTTPException, exc)
    return _problem(
        status_code=err.status_code,
        title=str(err.detail) if err.detail else "Error",
        problem_type="about:blank",
        detail=None,
        instance=request.url.path,
    )


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    return _problem(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        title="Validation error",
        problem_type=f"{_PROBLEM_BASE}/validation",
        detail="The request did not match the schema.",
        instance=request.url.path,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Wire the problem+json handlers onto the app."""
    app.add_exception_handler(DomainError, _domain_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
