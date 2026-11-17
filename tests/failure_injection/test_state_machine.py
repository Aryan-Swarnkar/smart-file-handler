"""process_file state machine under injected failures.

Kill-recovery, duplicate deliveries, stale events, and both terminal paths
(NonRetryableError, retry exhaustion) — each lands the file in a defined
state with a display-safe reason in files.error.
"""

from __future__ import annotations

import uuid

import pytest
from celery.exceptions import Retry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import File, FileStatus
from app.repositories.files import FileRepository
from app.repositories.quota import QuotaRepository
from app.workers import tasks as tasks_module
from tests.failure_injection.conftest import (
    FakeTask,
    run_task_body,
    seed_file,
    seed_user_with_quota,
)


async def status_of(db: AsyncSession, file_id: uuid.UUID) -> FileStatus | None:
    """Scalar read — immune to identity-map staleness across engines."""
    return await FileRepository(db).get_status(file_id)


# --- worker killed between the two transitions -----------------------------------


async def test_kill_between_steps_recovers_on_redelivery(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()

    # Simulate the kill: step 1 committed, process died before step 2.
    claimed = await FileRepository(db).mark_processing(file.id)
    assert claimed == 1
    await db.commit()
    assert await status_of(db, file.id) is FileStatus.PROCESSING

    # Redelivery: the full task body finishes the stuck file.
    await tasks_module._drive_state_machine(file.id)
    assert await status_of(db, file.id) is FileStatus.DONE


# --- duplicate deliveries ----------------------------------------------------------


async def test_duplicate_delivery_processed_once(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()

    await tasks_module._drive_state_machine(file.id)
    await tasks_module._drive_state_machine(file.id)  # republished duplicate
    assert await status_of(db, file.id) is FileStatus.DONE


@pytest.mark.parametrize(
    "status", [FileStatus.PENDING_UPLOAD, FileStatus.DONE, FileStatus.FAILED]
)
async def test_stale_event_on_non_participating_status_is_noop(
    db: AsyncSession, status: FileStatus
) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=status)
    await db.commit()

    await tasks_module._drive_state_machine(file.id)
    assert await status_of(db, file.id) is status  # untouched


async def test_file_deleted_before_task_runs_is_graceful(db: AsyncSession) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()
    await db.delete(file)
    await db.commit()

    await tasks_module._drive_state_machine(file.id)  # no raise, no resurrection
    assert await status_of(db, file.id) is None


# --- terminal path: NonRetryableError -----------------------------------------------


async def test_nonretryable_marks_failed_with_display_reason(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()
    reason = "format: image/svg+xml is not supported"

    async def boom(file_id: uuid.UUID) -> None:
        raise tasks_module.NonRetryableError(reason)

    monkeypatch.setattr(tasks_module, "_drive_state_machine", boom)
    run_task_body(FakeTask(retries=0, max_retries=5), str(file.id))  # no raise

    assert await status_of(db, file.id) is FileStatus.FAILED
    error = (
        await db.execute(select(File.error).where(File.id == file.id))
    ).scalar_one()
    assert error == reason


# --- retryable path -----------------------------------------------------------------


async def test_retryable_error_retries_with_backoff(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()

    async def flaky(file_id: uuid.UUID) -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(tasks_module, "_drive_state_machine", flaky)
    fake = FakeTask(retries=2, max_retries=5)
    with pytest.raises(Retry):  # the real celery retry signal
        run_task_body(fake, str(file.id))
    assert len(fake.retry_calls) == 1
    countdown = fake.retry_calls[0]
    assert 4.0 <= countdown < 5.0  # 2**2 + jitter[0,1)
    # No premature terminal write: the file keeps its status while retrying.
    assert await status_of(db, file.id) is FileStatus.UPLOADED


async def test_retry_exhaustion_marks_failed_with_reason(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await seed_user_with_quota(db)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()

    async def flaky(file_id: uuid.UUID) -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr(tasks_module, "_drive_state_machine", flaky)
    fake = FakeTask(retries=5, max_retries=5)  # final attempt
    run_task_body(fake, str(file.id))  # no raise — terminal, not crash

    assert fake.retry_calls == []  # exhausted: no more retries
    assert await status_of(db, file.id) is FileStatus.FAILED
    error = (
        await db.execute(select(File.error).where(File.id == file.id))
    ).scalar_one()
    assert error == (
        "processing failed after 6 attempts: RuntimeError: network down"
    )


async def test_exhausted_failed_file_leaves_the_stuck_set(db: AsyncSession) -> None:
    """Convergence proof for created_at-based stuck detection: rescue must
    terminate because terminal `failed` rows exit the rescue candidates."""
    user = await seed_user_with_quota(db)
    assert await QuotaRepository(db).reserve(user.id, 100)
    file = await seed_file(db, user, status=FileStatus.UPLOADED)
    await db.commit()
    await FileRepository(db).mark_failed(
        file.id, message="processing failed after 6 attempts: RuntimeError: boom"
    )
    await db.commit()

    stuck = await FileRepository(db).select_stuck(minutes=10)
    assert all(s.id != file.id for s in stuck)
    # ...and the quota reservation is NOT released by failure (trash/delete
    # semantics stay with the owner; a failed file still counts until Phase
    # 10 purge or explicit delete releases it).
    used = await QuotaRepository(db).used_bytes(user.id)
    assert used == 100
