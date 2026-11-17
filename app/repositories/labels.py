"""Label + file_labels queries.

ALL label SQL lives here. Labels are PER-USER (`uq_labels_user_id_name_kind`):
two users both processing a dog photo get two separate label rows, so label
names never leak across accounts and "files by label" is a per-user join.

get_or_create uses INSERT ... ON CONFLICT DO NOTHING + re-select (not
select-then-insert): two files of the same user processing concurrently can
race on the same new label name, and the constraint — not an app check —
referees that race.
"""

from __future__ import annotations

import uuid
from typing import NamedTuple

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FileLabel, Label, LabelKind


class LabelEntry(NamedTuple):
    """One label to attach to a file: (name, kind, confidence)."""

    name: str
    kind: LabelKind
    confidence: float


class LabelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self, *, user_id: uuid.UUID, entries: list[LabelEntry]
    ) -> dict[tuple[str, LabelKind], uuid.UUID]:
        """Existing-or-new label ids for (name, kind) pairs of ONE user.
        Caller commits; rows flushed here. Duplicate (name, kind) pairs in
        `entries` collapse to one label row (the first confidence wins in the
        caller's replace list)."""
        if not entries:
            return {}
        wanted = {(e.name, e.kind) for e in entries}

        existing = (
            await self._session.execute(
                select(Label.id, Label.name, Label.kind).where(
                    Label.user_id == user_id,
                    Label.name.in_({name for name, _ in wanted}),
                    Label.kind.in_({kind for _, kind in wanted}),
                )
            )
        ).all()
        resolved: dict[tuple[str, LabelKind], uuid.UUID] = {
            (name, kind): id for id, name, kind in existing
        }

        missing = [pair for pair in wanted if pair not in resolved]
        if missing:
            # ON CONFLICT DO NOTHING: a concurrent transaction may have
            # created the label after our SELECT — the constraint decides.
            await self._session.execute(
                pg_insert(Label)
                .values(
                    [
                        {"user_id": user_id, "name": name, "kind": kind}
                        for name, kind in missing
                    ]
                )
                .on_conflict_do_nothing(
                    index_elements=["user_id", "name", "kind"]
                )
            )
            # Re-select (covers the rows the conflict skipped AND the new ones).
            fresh = (
                await self._session.execute(
                    select(Label.id, Label.name, Label.kind).where(
                        Label.user_id == user_id,
                        Label.name.in_({name for name, _ in missing}),
                        Label.kind.in_({kind for _, kind in missing}),
                    )
                )
            ).all()
            resolved.update({(name, kind): id for id, name, kind in fresh})
        return resolved

    async def replace_file_labels(
        self,
        *,
        file_id: uuid.UUID,
        entries: list[tuple[uuid.UUID, float]],
    ) -> int:
        """One file's label set = exactly `entries`: wipe + bulk insert.
        Runs in the caller's transaction (idempotent under reprocessing —
        the same labels produce the same final set). Returns rows written."""
        await self._session.execute(delete(FileLabel).where(FileLabel.file_id == file_id))
        if not entries:
            return 0
        await self._session.execute(
            insert(FileLabel),
            [
                {"file_id": file_id, "label_id": label_id, "confidence": confidence}
                for label_id, confidence in entries
            ],
        )
        return len(entries)
