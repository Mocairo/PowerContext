"""Request identifiers and the one error envelope every API response is allowed to use.

Spec 17.2 fixes a single failure shape — ``{"error": {"code", "message", "details"},
"request_id"}`` — and 17.3 requires the identifier on every error so an operator can tie a
response a client saw back to one log line. The identifier is minted here per request and echoed
as ``X-LabRelay-Request-Id``; a client-supplied header is deliberately never echoed, because that
would put untrusted text into a response header and into the log. The header name is pinned to
``configs/app.yaml`` by ``tests/unit/test_api_core.py``, the same way Task 3 pinned the importer
constants and Task 4 the retrieval weights.

This module is also the only place an unexpected exception becomes a response. The middleware
wraps the routes, so a store or gateway that blows up yields a clean 500 with a stable code
rather than a traceback, and only the exception *class* is ever logged — the rule Task 5 set for
memory failures, because a dependency's message can echo a URL or a key (spec 19.1).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

REQUEST_ID_HEADER = "X-LabRelay-Request-Id"
REQUEST_ID_PREFIX = "request-"

# The stable ``error.code`` values Task 6 and Task 14 share. Codes, not HTTP statuses, are what
# the UI branches on, so a code must never be inferred from a message.
INTERNAL_ERROR = "internal_error"
VALIDATION_FAILED = "validation_failed"
NOT_FOUND = "not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"
PROJECT_NOT_FOUND = "project_not_found"
HANDOFF_NOT_FOUND = "handoff_not_found"
HANDOFF_NOT_READY = "handoff_not_ready"
IMPORT_PATH_NOT_DIRECTORY = "import_path_not_directory"
ASSESSMENT_NOT_FOUND = "assessment_not_found"
PLAN_NOT_FOUND = "plan_not_found"
PLAN_VALIDATION_FAILED = "plan_validation_failed"
DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
SERVICE_NOT_CONFIGURED = "service_not_configured"

# Framework-raised statuses (an unknown route, a wrong method, a body FastAPI itself refused)
# still have to answer in the envelope, so they are mapped to codes here rather than in a route.
_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    404: NOT_FOUND,
    405: METHOD_NOT_ALLOWED,
    409: "conflict",
    413: "request_too_large",
    415: "unsupported_media_type",
    422: VALIDATION_FAILED,
    429: "rate_limited",
    500: INTERNAL_ERROR,
    503: DEPENDENCY_UNAVAILABLE,
}

GENERIC_FAILURE_MESSAGE = (
    "An unexpected error stopped this request. Quote the request id when asking for help."
)

logger = logging.getLogger("labrelay.api.errors")


class ApiError(Exception):
    """A route-level failure that already knows its status, code, message and safe details.

    ``details`` carries identifiers the client needs (a project id, a resolved path) and never
    exception text: this is what the API promises to show, so it is written at the raise site.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


def new_request_id() -> str:
    return f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex}"


def request_id_of(request: Request) -> str:
    """This request's identifier, minted on the fly if the middleware did not run.

    A router mounted on a bare ``FastAPI()`` (the plan's own snippet) has no middleware, so the
    envelope must still carry an id rather than ``null``.
    """
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    request.state.request_id = new_request_id()
    return request.state.request_id


def error_payload(
    request: Request, code: str, message: str, details: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": dict(details or {})},
        "request_id": request_id_of(request),
    }


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """One envelope body plus the request-id header, so no handler can forget either."""
    payload = error_payload(request, code, message, details)
    response_headers = {**(headers or {}), REQUEST_ID_HEADER: payload["request_id"]}
    return JSONResponse(status_code=status_code, content=payload, headers=response_headers)


def _field_location(error: Mapping[str, Any]) -> str:
    """``("body", "title")`` -> ``"body.title"``, the dotted path the UI highlights a field by."""
    return ".".join(str(part) for part in error.get("loc") or ())


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Re-shapes FastAPI/Starlette's own ``{"detail": ...}`` answers into the envelope."""
    code = _CODE_BY_STATUS.get(exc.status_code, "http_error")
    message = exc.detail if isinstance(exc.detail, str) else "The request could not be handled."
    return error_response(request, exc.status_code, code, message, headers=exc.headers)


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    """Field path and error type only: the rejected input may be a credential the caller typed."""
    errors = [
        {"loc": _field_location(error), "type": str(error.get("type", "invalid"))}
        for error in exc.errors()
    ]
    return error_response(
        request, 422, VALIDATION_FAILED, "The request body failed validation.", {"errors": errors}
    )


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Mints one identifier per request, echoes it on every response and owns the 500 path."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = new_request_id()
        try:
            response = await call_next(request)
        except ApiError as exc:
            # An expected, already-described failure: the route decided the code, nothing to log.
            response = error_response(request, exc.status_code, exc.code, exc.message, exc.details)
        except Exception as exc:
            logger.error(
                "unhandled error operation=http_request request_id=%s method=%s path=%s "
                "exception_class=%s",
                request.state.request_id,
                request.method,
                request.url.path,
                type(exc).__name__,
            )
            response = error_response(request, 500, INTERNAL_ERROR, GENERIC_FAILURE_MESSAGE)
        response.headers[REQUEST_ID_HEADER] = request_id_of(request)
        return response


def configure_error_surface(app: FastAPI) -> None:
    """Attaches the identifier middleware and the envelope handlers to ``app``."""
    app.add_middleware(RequestIdMiddleware)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
