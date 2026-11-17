"""Outbox writes — the ONLY bridge from DB state to Celery.

The API inserts rows in the SAME transaction as the state change they
announce; the relay (Phase 6) claims unpublished rows with
SELECT ... FOR UPDATE SKIP LOCKED and publishes them. At-least-once
delivery: consumers must be idempotent.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, NamedTuple, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Outbox


class OutboxClaim(NamedTuple):
    """One row handed to the relay. `attempts` is the post-increment value
    (the count of claim cycles this row has survived)."""

    id: int
    event_type: str
    payload: dict[str, Any]
    attempts: int


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_event(
        self, *, event_type: str, payload: dict[str, Any]
    ) -> Outbox:
        """Flush (no commit) — the caller's transaction makes the state
        change and its announcement atomic."""
        row = Outbox(event_type=event_type, payload=payload)
        self._session.add(row)
        await self._session.flush()
        return row

    async def claim_unpublished(self, *, limit: int) -> list[OutboxClaim]:
        """Claim up to `limit` unpublished rows for THIS relay instance:
        SELECT ... FOR UPDATE SKIP LOCKED, then attempts++ on exactly those
        rows — same transaction, which the CALLER commits before any broker
        I/O (row locks must never span a network call).

        Two relays racing get disjoint sets at that instant; a row that is
        re-claimed later (published_at still NULL — crash between send and
        stamp) is republished: at-least-once, made harmless by idempotent
        tasks. Oldest-first via the partial unpublished index."""
        rows = (
            await self._session.execute(
                select(Outbox.id, Outbox.event_type, Outbox.payload, Outbox.attempts)
                .where(Outbox.published_at.is_(None))
                .order_by(Outbox.created_at.asc(), Outbox.id.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not rows:
            return []
        await self._session.execute(
            update(Outbox)
            .where(Outbox.id.in_([r.id for r in rows]))
            .values(attempts=Outbox.attempts + 1)
        )
        return [
            OutboxClaim(
                id=r.id, event_type=r.event_type, payload=r.payload, attempts=r.attempts + 1
            )
            for r in rows
        ]

    async def stamp_published(self, ids: Sequence[int]) -> int:
        """Stamp the rows whose send succeeded. `published_at IS NULL` keeps
        the write idempotent (a double stamp can never overwrite)."""
        if not ids:
            return 0
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(Outbox)
                .where(Outbox.id.in_(list(ids)), Outbox.published_at.is_(None))
                .values(published_at=func.now())
            ),
        )
        return result.rowcount

    async def has_unpublished_event(
        self, *, event_type: str, file_id: uuid.UUID
    ) -> bool:
        """True while an unpublished event of this type still targets the
        file — the sweeper's rescue dedupe (worker down for hours must not
        stack hundreds of reprocess rows for the same file)."""
        stmt = (
            select(Outbox.id)
            .where(
                Outbox.event_type == event_type,
                Outbox.published_at.is_(None),
                Outbox.payload["file_id"].astext == str(file_id),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None
