"""Unit tests for the Phase 3 token-bucket limiter (real Redis, db 15).

The Lua script is exercised through the same redis-py Script wrapper the API
uses, against the compose Redis. Keys live under the run's RATELIMIT
namespace, so the autouse conftest flush sweeps them between tests; bucket
names are still made unique per test for defense in depth.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import AsyncGenerator, Coroutine
from typing import Any, cast

import pytest
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.ratelimit import TokenBucketLimiter, format_retry_after


@pytest.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    client = Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def limiter(redis_client: Redis) -> TokenBucketLimiter:
    return TokenBucketLimiter(redis_client, get_settings().RATELIMIT_NAMESPACE)


def _bucket() -> str:
    return f"t-{uuid.uuid4().hex[:8]}"


async def test_capacity_spends_then_denies(limiter: TokenBucketLimiter) -> None:
    bucket = _bucket()
    results = [await limiter.hit(bucket, "a@x.com", "1.1.1.1", 5) for _ in range(6)]
    assert all(r.allowed for r in results[:5])
    assert not results[5].allowed
    assert results[5].remaining == 0
    # 5/min -> refill 1 per 12s -> retry is 12s, always reported >= 1s.
    assert 1 <= results[5].retry_after_seconds <= 60


async def test_email_and_ip_are_separate_buckets(limiter: TokenBucketLimiter) -> None:
    bucket = _bucket()
    drained = [await limiter.hit(bucket, "a@x.com", "1.1.1.1", 1) for _ in range(2)]
    assert drained[0].allowed and not drained[1].allowed
    # Different email, same IP: independent bucket.
    assert (await limiter.hit(bucket, "b@x.com", "1.1.1.1", 1)).allowed
    # Same email, different IP: independent bucket.
    assert (await limiter.hit(bucket, "a@x.com", "2.2.2.2", 1)).allowed


async def test_empty_email_is_shared_sentinel_per_ip(
    limiter: TokenBucketLimiter,
) -> None:
    """/refresh with an unknown token passes email="" — one bucket per IP."""
    bucket = _bucket()
    first = await limiter.hit(bucket, "", "3.3.3.3", 1)
    second = await limiter.hit(bucket, "", "3.3.3.3", 1)
    other_ip = await limiter.hit(bucket, "", "4.4.4.4", 1)
    assert first.allowed and not second.allowed
    assert other_ip.allowed


async def test_refill_accrues_from_elapsed_time(
    limiter: TokenBucketLimiter, redis_client: Redis
) -> None:
    """Pre-seed a drained bucket with a timestamp 1.1s in the past and a
    60/min refill (1 token/s): the script must top it up to >= 1 token."""
    bucket = _bucket()
    key = limiter.key(bucket, "c@x.com", "5.5.5.5")
    # redis-py stubs type hset as the sync|async union; in an async client it
    # is a coroutine — narrow it so the await typechecks.
    hset = cast("Coroutine[Any, Any, int]", redis_client.hset(
        key, mapping={"tokens": "0", "ts": str(int(time.time() * 1000) - 1100)}
    ))
    await hset
    result = await limiter.hit(bucket, "c@x.com", "5.5.5.5", 60)
    assert result.allowed


async def test_key_shape_is_namespaced_hashed(limiter: TokenBucketLimiter) -> None:
    key = limiter.key("login", "user@example.com", "127.0.0.1")
    email_hash = hashlib.sha256(b"user@example.com").hexdigest()
    assert key == (
        f"{get_settings().RATELIMIT_NAMESPACE}:auth:login:{email_hash}:127.0.0.1"
    )


async def test_fail_open_when_redis_down() -> None:
    """Redis unreachable -> allow (auth availability beats strict limiting)."""
    dead = TokenBucketLimiter(
        Redis.from_url("redis://localhost:9/0", socket_connect_timeout=1),
        "dead-ns",
    )
    result = await dead.hit(_bucket(), "d@x.com", "6.6.6.6", 5)
    assert result.allowed


def test_format_retry_after_wording() -> None:
    assert format_retry_after(1) == "1 second"
    assert format_retry_after(29) == "29 seconds"
    assert format_retry_after(60) == "1 minute"
    assert format_retry_after(90) == "1 minute"  # 90s rounds to 1 minute
    assert format_retry_after(120) == "2 minutes"
