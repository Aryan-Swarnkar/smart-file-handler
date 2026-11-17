"""Image pipeline: bomb guard -> labels -> replace file_labels (one tx).

Order matters:
1. Header probe FIRST (cheap, no decode): `Image.open` parses only the
   header, so a 69-byte PNG claiming 60000x60000 pixels is rejected BEFORE
   any decompression (decompression-bomb guard) and garbage bytes fail here
   with a deterministic "format:" reason instead of looping through retries.
2. Registry labels (stub: deterministic fake; real: full decode + CLIP —
   Pillow's own MAX_IMAGE_PIXELS stays armed as the decode-time safety net
   for anything the header probe could not see).
3. Labels are PER-USER: get-or-create (kind=THING) + replace the file's
   file_labels in ONE transaction — reprocessing replaces, never duplicates.

Pillow is a required dependency (13.x, MIT-CMU); the probe never decodes.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models import LabelKind
from app.repositories.labels import LabelEntry, LabelRepository
from app.workers.errors import NonRetryableError
from app.workers.pipelines.models_registry import get_registry

logger = get_logger(__name__)

# Decode-time safety net: an image whose header parses below the limit but
# whose REAL raster exceeds it (crafted headers) dies in Pillow's own guard.
# Set once at import; stricter than Pillow's 89 MP default.
Image.MAX_IMAGE_PIXELS = get_settings().MAX_IMAGE_PIXELS


def assert_image_ok(path: Path) -> tuple[int, int]:
    """Header-only probe (no decode). Sync — the caller runs it in a thread.

    Raises NonRetryableError for bombs (declared pixels above the limit) and
    unidentifiable bytes; everything else (transient I/O) propagates and
    stays retryable."""
    try:
        with Image.open(path) as probe:
            width, height = probe.size
    except UnidentifiedImageError as exc:
        raise NonRetryableError("format: unreadable image data") from exc
    except DecompressionBombError as exc:
        raise NonRetryableError(
            f"format: image exceeds the {get_settings().MAX_IMAGE_PIXELS}-pixel "
            "processing limit"
        ) from exc
    if width * height > get_settings().MAX_IMAGE_PIXELS:
        raise NonRetryableError(
            f"format: image is {width}x{height} pixels, above the "
            f"{get_settings().MAX_IMAGE_PIXELS}-pixel processing limit"
        )
    return width, height


async def run_image_pipeline(
    factory: async_sessionmaker,
    *,
    file_id: uuid.UUID,
    user_id: uuid.UUID,
    path: Path,
) -> list[str]:
    """One image file -> its (per-user) label set, persisted. Returns the
    label NAMES for logging. Raises NonRetryableError for bad files."""
    # 1. Bomb/format guard — in a thread: Pillow reads from disk (rule 8).
    await asyncio.to_thread(assert_image_ok, path)

    # 2. Labels. Stub: deterministic, no decode. Real: decode + CLIP inside.
    registry = get_registry()
    result = await registry.label_image(file_id, path)
    entries = [
        LabelEntry(name=name, kind=LabelKind.THING, confidence=confidence)
        for name, confidence in result.labels
    ]

    # 3. Persist in ONE transaction: get-or-create + replace.
    async with factory() as session:
        labels = LabelRepository(session)
        resolved = await labels.get_or_create(user_id=user_id, entries=entries)
        seen: set[tuple[str, LabelKind]] = set()
        pairs: list[tuple[uuid.UUID, float]] = []
        for entry in entries:
            key = (entry.name, entry.kind)
            if key in seen:  # first confidence wins; file_labels PK is
                continue  # (file_id, label_id) — duplicates would collide
            seen.add(key)
            pairs.append((resolved[key], entry.confidence))
        await labels.replace_file_labels(file_id=file_id, entries=pairs)
        await session.commit()

    names = [name for name, _ in result.labels]
    logger.info("image_labels_written", file_id=str(file_id), labels=names)
    return names
