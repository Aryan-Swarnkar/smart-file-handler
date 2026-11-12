"""Fixed-window rate limiter: Redis INCR + EXPIRE, one key per (scope, IP).

Why fixed window (vs sliding log or token bucket): O(1) per check, one
round-trip, and Redis holds the counters — correct across N stateless API
replicas behind a load balancer. Trade-off: up to 2x `limit` can slip through
around a window edge; acceptable for credential-stuffing defense (the argon2
cost per attempt is the real brake).

Fail-open policy: if Redis is unreachable we ALLOW the request and log a
warning. Rationale: auth availability beats strict limiting, and password
verify cost still throttles brute force at the DB layer.
"""

from __future__ import annotations

from typing import NamedTuple

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger

logger = get_logger(__name__)


class RateLimitResult(NamedTuple):
    allowed: bool
    limit: int
    remaining: int
    reset_after: int  # seconds until the current window expires


class RateLimiter:
    def __init__(self, redis: Redis, namespace: str) -> None:
        self._redis = redis
        self._namespace = namespace

    async def hit(
        self, scope: str, identifier: str, limit: int, window_seconds: int
    ) -> RateLimitResult:
        key = f"{self._namespace}:{scope}:{identifier}"
        try:
            count = int(await self._redis.incr(key))
            if count == 1:
                # First hit in this window: start the TTL. (INCR creates the
                # key without one; without this the window would be forever.)
                await self._redis.expire(key, window_seconds)
                ttl = window_seconds
            else:
                raw_ttl = await self._redis.ttl(key)
                # -1 (no expiry) can race with the expire above on a fresh
                # key; repair instead of trusting it.
                if raw_ttl < 0:
                    await self._redis.expire(key, window_seconds)
                    ttl = window_seconds
                else:
                    ttl = raw_ttl
            return RateLimitResult(
                allowed=count <= limit,
                limit=limit,
                remaining=max(0, limit - count),
                reset_after=max(0, ttl),
            )
        except (RedisError, OSError):
            logger.warning("rate_limiter_unavailable", scope=scope, key=key)
            return RateLimitResult(True, limit, limit, 0)
