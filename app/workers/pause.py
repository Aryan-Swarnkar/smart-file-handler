"""Pipeline pause — ONE advisory lock, TWO gates (relay claims + beat sweep).

`pg_advisory_lock(<key>)` (session-level) pauses the ASYNC pipeline: the
relay makes no NEW claims and beat's sweep_once skips its run, while
in-flight cycles finish normally. Legitimate uses:

- ops: pause the pipeline during migrations/maintenance without stopping
  containers (`SELECT pg_advisory_lock(1380205633);` in a psql session —
  keep it open, release with pg_advisory_unlock or by disconnecting);
- tests: pytest shares the dev database with the LIVE dev stack — holding
  this lock for the duration of each test makes the suite deterministic
  against the live relay/worker instead of racing them.

Gates PROBE the lock (try + immediate release, so a sweep must never pause
the relay and vice versa): a holder must keep it held CONTINUOUSLY; the
microsecond window between a probe and the next claim is accepted — pause
is for maintenance windows and test isolation, not hard isolation.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

# 'RELA' as ASCII — stable, greppable, and distinct from this app's other
# advisory-lock users (idempotency uses xact locks derived from digests).
PIPELINE_PAUSE_LOCK_KEY = 0x52454C41


async def pipeline_clear() -> bool:
    """Probe the pause lock: True = clear to claim/sweep. The lock is
    acquired and immediately released on a throwaway connection (NullPool
    closes it, which also auto-releases any leaked lock)."""
    engine = create_async_engine(get_settings().DATABASE_URL, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            acquired = (
                await session.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": PIPELINE_PAUSE_LOCK_KEY},
                )
            ).scalar_one()
            if not acquired:
                return False
            await session.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": PIPELINE_PAUSE_LOCK_KEY},
            )
            return True
    finally:
        await engine.dispose()
