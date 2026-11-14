"""Redis token-bucket rate limiter — one atomic Lua EVAL per request.

Phase 3 spec: `core/ratelimit.py`, used on login/register/refresh, keyed by
IP+email, 429 with Retry-After. Token bucket replaces the v2 fixed window:
a burst within one minute is allowed up to the capacity, then refills
continuously (knob/60 per second) — same average rate, no window-edge 2x.

ATOMICITY: refill + spend + persist is a single Lua script, so two API
replicas sharing Redis cannot double-spend (the v2 INCR-then-EXPIRE dance
was two round-trips with a repair branch for the race between them).

KEY SHAPE: `{namespace}:auth:{bucket}:{sha256(email)}:{ip}` — the email is
hashed so no raw PII sits in Redis, and the key stays bounded. An EMPTY
email hashes to a fixed sentinel bucket: /refresh bodies carry no email, so
unknown/garbage tokens (no resolvable owner) share that one bucket per IP —
attackers cannot mint fresh buckets by inventing emails.

FAIL-OPEN: if Redis is unreachable we ALLOW the request and log a warning.
Auth availability beats strict limiting, and argon2 verify cost still
throttles brute force. Same policy as the v2 limiter.
"""

from __future__ import annotations

import hashlib
import time
from typing import NamedTuple

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger

logger = get_logger(__name__)

# Idle buckets expire after 2 minutes: every knob is per-minute, so an idle
# bucket is provably full again within 60s and the key is dead weight after.
_BUCKET_TTL_MS = 120_000

# One request costs one token.
_COST = 1

# Refill + spend + persist in ONE Redis call. KEYS[1] = bucket key;
# ARGV: capacity, refill_per_sec, now_ms, ttl_ms. Returns
# {allowed(0|1), remaining_tokens(floor), retry_after_ms}.
_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
    tokens = capacity
    ts = now_ms
end

local elapsed_ms = now_ms - ts
if elapsed_ms > 0 then
    tokens = math.min(capacity, tokens + elapsed_ms / 1000.0 * refill_per_sec)
end

local allowed = 1
if tokens < 1 then
    allowed = 0
else
    tokens = tokens - 1
end

redis.call('HMSET', key, 'tokens', tostring(tokens), 'ts', tostring(now_ms))
redis.call('PEXPIRE', key, ttl_ms)

local retry_after_ms = 0
if allowed == 0 then
    retry_after_ms = math.ceil((1 - tokens) / refill_per_sec * 1000)
end
return {allowed, tostring(math.floor(tokens)), retry_after_ms}
"""


class RateLimitResult(NamedTuple):
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


def format_retry_after(seconds: int) -> str:
    """Human wording for the 429 body: '29 seconds' / '1 minute'."""
    if seconds < 60:
        unit = "second" if seconds == 1 else "seconds"
        return f"{seconds} {unit}"
    minutes = max(1, seconds // 60)  # partial minutes round up to 1+
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit}"


class TokenBucketLimiter:
    def __init__(self, redis: Redis, namespace: str) -> None:
        self._redis = redis
        self._namespace = namespace
        # Registered once per limiter instance; redis-py sends EVALSHA with an
        # automatic EVAL fallback after a script flush.
        self._script = self._redis.register_script(_LUA)

    def key(self, bucket: str, email: str, ip: str) -> str:
        """Deterministic bucket key (public: conftest + tests assert on it)."""
        email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
        return f"{self._namespace}:auth:{bucket}:{email_hash}:{ip}"

    async def hit(
        self, bucket: str, email: str, ip: str, per_minute: int
    ) -> RateLimitResult:
        """Spend one token from the (bucket, email, ip) bucket."""
        key = self.key(bucket, email, ip)
        refill_per_sec = per_minute / 60.0
        now_ms = int(time.time() * 1000.0)
        try:
            allowed_raw, remaining_raw, retry_ms = await self._script(
                keys=[key],
                args=[per_minute, refill_per_sec, now_ms, _BUCKET_TTL_MS],
            )
        except (RedisError, OSError):
            logger.warning("rate_limiter_unavailable", bucket=bucket)
            return RateLimitResult(True, per_minute, per_minute, 0)

        allowed = bool(int(allowed_raw))
        # ceil of the Lua's ms; floored at 1 (Retry-After: 0 is meaningless).
        retry_after_seconds = (
            0 if allowed else max(1, -(-int(retry_ms) // 1000))
        )
        return RateLimitResult(
            allowed=allowed,
            limit=per_minute,
            remaining=int(remaining_raw),
            retry_after_seconds=retry_after_seconds,
        )
