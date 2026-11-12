"""Structured logging (structlog) + request-ID propagation.

Design notes:
- A pure-ASGI middleware is used instead of BaseHTTPMiddleware: the latter is
  known to break SSE/streaming responses and BackgroundTasks, and we plan SSE
  later. Pure ASGI has none of those failure modes.
- request_id travels in a contextvar so every log line inside the request (and
  exception handlers, which run inside the middleware) carries it.
- uvicorn's own access/error logs are routed through the same formatter so ALL
  stdout lines are uniform JSON in production.
"""

from __future__ import annotations

import contextvars
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import Processor

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

ASGIAppT = Callable[[Scope, Receive, Send], Awaitable[None]]


def new_request_id() -> str:
    # hex form: compact, URL-safe, no dashes to break naive log greps.
    return uuid.uuid4().hex


def _add_request_id(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    event_dict.setdefault("request_id", request_id_var.get())
    return event_dict


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.dict_tracebacks,
        _add_request_id,
    ]


def _resolve_level(level: str) -> int:
    return logging.getLevelNamesMapping().get(level.upper(), logging.INFO)


def configure_logging(
    level: str = "INFO",
    log_format: Literal["json", "console"] = "json",
) -> None:
    """Idempotent logging setup called once at app creation."""
    renderer: Processor
    if log_format == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer()

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]  # replace, never stack up duplicate handlers
    root.setLevel(_resolve_level(level))

    # Route uvicorn internals through the same formatter.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


class RequestIdMiddleware:
    """Assigns/propagates X-Request-ID and stamps it onto every log line."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id")
        # Accept a client-supplied ID only if sane length; never trust its content.
        request_id = (
            incoming[:64] if incoming and 0 < len(incoming) <= 64 else new_request_id()
        )

        token = request_id_var.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("x-request-id", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            request_id_var.reset(token)
