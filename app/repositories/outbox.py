"""Outbox writes — the ONLY bridge from DB state to Celery.

The API inserts rows in the SAME transaction as the state change they
announce; the relay (Phase 6) claims unpublished rows with
SELECT ... FOR UPDATE SKIP LOCKED and publishes them. At-least-once
delivery: consumers must be idempotent.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Outbox


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
