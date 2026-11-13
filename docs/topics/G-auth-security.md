# G — Auth & security (Phase 2)

Code: `app/services/security.py`, `app/services/auth.py`, `app/api/deps.py`,
`app/api/routers/auth.py`, `app/schemas/auth.py`,
`app/repositories/{users,refresh_tokens}.py`, `tests/test_auth.py`.

## The two-token model (why)

- **Access token** — HS256 JWT, 15 min, carries `sub` (user uuid), `iat`,
  `exp`, `typ=access`. Stateless: every protected route decodes it locally —
  no DB hit for auth, which is what lets us run N stateless API replicas
  with zero shared session state. Trade-off: revocation is not instant; the
  15-min expiry is the bound (logout kills the REFRESH token, not the access).
- **Refresh token** — opaque 256-bit random (`secrets.token_urlsafe(32)`),
  7 days, single-use. Opaque beats JWT for refresh: the DB lookup happens
  anyway (rotation), so carrying claims buys nothing and a JWT payload would
  leak user data on every refresh. Only SHA-256(raw) is stored — a DB dump
  cannot mint sessions.

## Password storage: argon2id

`argon2.PasswordHasher()` defaults = 64 MiB memory, 3 iterations, 4 lanes.
Memory-hardness is the point: GPU/ASIC cracking costs scale with memory.
`verify()` returns bool; corrupted stored hashes are caught (`Argon2Error`)
and treated as failed logins, never 500s.

**User-enumeration defense**: unknown email and wrong password produce the
byte-identical 401 (`invalid_credentials`). For unknown emails we call
`dummy_verify()` — a real argon2 verify against a throwaway hash — so wall
time matches the known-user path. Without it, response-time deltas would
reveal which emails are registered.

## Refresh rotation + reuse detection (OAuth BCP)

```
issue A ─▶ use A ─▶ issue B, A revoked (replaced_by_id = B)
                    └─ A presented again ─▶ THEFT: revoke ALL live tokens
                                            of that user, log, 401
```

- Rotation shrinks the window a stolen token is useful to ~one request.
- Reuse detection turns the replay itself into the alarm: the whole family
  dies, attacker and victim both get logged out, victim re-logs in.
- `replaced_by_id` chain = full audit trail of every session lineage.
- Hash lookups are unique-indexed (`token_hash`) — O(1).

## Logout (P2.1 — RFC 7009 idempotent)

`POST /auth/logout` **always returns 204**. Live token → revoked. Unknown /
already-revoked / garbage → silent no-op. Presented token is `.strip()`ed.

Why this is correct (and better than strict 401):

- **Client UX**: logout can never fail. Before the fix, `refresh` → `logout`
  with the old token returned 401 because rotation had already killed it —
  a real user-reported bug (2026-11-13). Same after a reuse-detection
  family nuke.
- **Oracle elimination**: a uniform 204 removes the only way to probe
  "does this token hash exist in the DB?" (the old 401 leaked that bit).
- RFC 7009 (OAuth Token Revocation) explicitly recommends responding 200
  even for invalid tokens at revocation endpoints for this reason.

Trade-off: a client passing garbage "believes" it logged out — but the
token it holds is useless for anything else anyway; nothing is lost.

## Rate limiting (credential stuffing)

Fixed window: Redis `INCR rl:{ns}:{scope}:{ip}` + `EXPIRE 60` on first hit.
Redis counters (not process memory) → correct with N API replicas behind an
LB, which in-process limits (slowapi per-worker dicts) are not. 429 carries
`Retry-After` (seconds until window resets) AND a human message in the body:
`"Too many login attempts. You can try again in 29 seconds."` + `details.
retry_after_seconds` — machines parse the header, users read the message.
Fail-open on Redis outage: availability of auth beats strict limiting;
argon2 cost remains the per-attempt brake.

## Request validation

pydantic v2 schemas are the security boundary for shape AND size:
`EmailStr` (email-validator), `SecretStr` password (min 8 / max 128 — min is
a sanity floor; argon2 does the real work), `Field(min_length=1)` on token
payloads. Response models (`UserOut`, `TokenPair`) guarantee password/secret
material can never leak — there is no code path that serializes it.

## HTTPS & transport (P9 notes, decided now)

- Cookies are NOT used for tokens (Authorization header — no CSRF surface).
  Browsers must send `Authorization: Bearer`, which is immune to CSRF.
- TLS termination at the LB in production; HSTS in P9 checklist.

## What is deliberately NOT here (documented gaps)

- No email verification / password reset (single-user-focus MVP; reset flow
  would be a P10+ addition reusing the same token machinery).
- No 2FA/WebAuthn. No OAuth social login.
- No token blacklist (see logout trade-off above).
- Rate limiter keyed on `request.client.host`; X-Forwarded-For trust arrives
  with the deployment phase (spoofable if trusted naively).
