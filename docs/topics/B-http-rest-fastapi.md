# B — HTTP / REST / FastAPI (Phase 2, exercised heavily; started P0)

Code: `app/api/routers/{health,auth}.py`, `app/api/deps.py`,
`app/schemas/auth.py`, `app/core/errors.py`.

## REST shape of this API

| Method | Path | Success | Errors |
|---|---|---|---|
| GET | /healthz | 200 | — |
| GET | /readyz | 200 / 503 | names failing deps |
| GET | /metrics | 200 | — |
| POST | /auth/register | 201 UserOut | 409 email_taken, 422 validation, 429 rate_limited |
| POST | /auth/login | 200 TokenPair | 401 invalid_credentials, 429 |
| POST | /auth/refresh | 200 TokenPair (new pair) | 401 invalid_refresh_token |
| POST | /auth/logout | 204 | 401 invalid_refresh_token |
| GET | /auth/me | 200 UserOut | 401 unauthorized |

Conventions: nouns not verbs in paths, plural resources, proper status codes
(201 created, 204 no content, 401 vs 409 vs 422 distinct meanings), one JSON
error envelope for every failure (`error.code` matchable), no auth state in
cookies (Authorization header only → no CSRF surface).

## FastAPI mechanics this codebase uses

**Dependency injection (the framework's core superpower).** Dependencies are
plain async functions; FastAPI resolves them per request, caches within one
request, and chains them:

```
router endpoint
  ├─ Depends(get_session)          → one AsyncSession, closed after request
  ├─ Depends(auth_service)         → AuthService(session) [reuses cached session]
  ├─ Depends(get_current_user)     → HTTPBearer → decode JWT → session.get(User)
  └─ Depends(rate_limit(...))      → factory returning a dependency closure
```

`Annotated[T, Depends(...)]` style everywhere (3.12-idiomatic, mypy-clean,
works with OpenAPI).

**Why `HTTPBearer(auto_error=False)`**: default FastAPI returns its own 403
"Not authenticated" for a missing header — a different shape than our
envelope and semantically wrong (403 = authenticated-but-forbidden). With
`auto_error=False` we raise `UnauthorizedError` (401 + our envelope) and can
distinguish "missing" from "invalid".

**Why `Depends()` on a class fails here**: FastAPI would try to build
`AuthService` itself, treating its `__init__(session: AsyncSession)` as a
sub-dependency and AsyncSession as an invalid Pydantic field. Explicit
provider functions (`auth_service`) fix that — also why repos are NOT
injected directly into routers.

**Request/response models.** Input: `RegisterIn`/`LoginIn`/`RefreshIn`
(pydantic v2, SecretStr so passwords never appear in repr/logs/docs).
Output: `response_model=UserOut/TokenPair` — FastAPI serializes THROUGH the
model, so accidental attributes cannot leak. Validation failures become 422
with our envelope via the `RequestValidationError` handler.

**Status codes & responses**: `status.HTTP_201_CREATED` on register,
204 + `None` return for logout (no body), JSONResponse for readyz so we can
flip 200/503 dynamically, custom `headers=...` on AppError (Retry-After).

**App lifecycle**: lifespan context manages redis pool + engine dispose
(startup/shutdown hooks), `create_app()` factory for tests, routers
registered on the factory. ASGI middleware: RequestIdMiddleware is pure
ASGI (runs outside routing, no BaseHTTPMiddleware threadpool cost).

## Error envelope contract

```json
{"error": {"code": "...", "message": "...", "details": ...},
 "request_id": "..."}
```

Handlers installed once in `install_error_handlers`: AppError (ours),
StarletteHTTPException (mapped into the same shape), RequestValidationError
(422 + details), Exception (500, logged with traceback, internals never
leaked). `request_id` ties the response to its structlog line — P8 dashboards
correlate on it.

## What's coming (later phases)

- P3: resource routers for files with keyset pagination (cursor in query
  params, 404-not-403 ownership), ETags not needed (no concurrent edits).
- P8: RED metrics per route, slow-request logging middleware.
