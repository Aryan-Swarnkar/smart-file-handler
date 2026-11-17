"""Content sniffing: decide a file's kind from its actual BYTES, not its name.

v3 allowlist (locked by the Phase 5 prompt): magic bytes against
jpeg / png / webp / gif / pdf; anything else -> kind=other (stored, browsable,
doc_topic="other" — nothing to process). A fake .jpg that is really an .exe
must never reach the image pipeline: sniff wins over the declared type.

SVG/HTML rejection arrives with the Phase 5 complete-upload rewrite; today
their bytes simply classify as other. Pure functions only — no I/O, no ML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.db.models.file import FileKind

# How many leading bytes to sample for sniffing (magic is ≤16 bytes).
SNIFF_SAMPLE_BYTES = 8192

# JPEG SOI + APP0 marker; PNG signature; RIFF container with WEBP form type;
# GIF87a/GIF89a headers; PDF header.
_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"
_GIF = (b"GIF87a", b"GIF89a")
_PDF = b"%PDF-"


@dataclass(frozen=True, slots=True)
class SniffResult:
    kind: FileKind
    mime_type: str


def sanitize_extension(filename: str) -> str:
    """Lowercased alnum extension, or '' — used for the S3 key + routing."""
    m = re.search(r"\.([A-Za-z0-9]{1,10})$", filename.strip())
    return m.group(1).lower() if m else ""


def sanitize_filename(filename: str) -> str:
    """Basename only, hard cap 255 (DB column) — we never touch a filesystem,
    but names end up in JSON responses and the DB."""
    base = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return base[:255] or "unnamed"


# Markup signatures the allowlist must REJECT at complete (Phase 5): SVG can
# carry scripted payloads and HTML can phish — they must never become kind=other
# and re-serve inline. Case-insensitive, whitespace-tolerant probes.
_MARKUP_PROBES = ("<svg", "<html", "<!doctype html", "<?xml")


def is_rejected_markup(head: bytes) -> bool:
    """True when the sampled bytes look like SVG/HTML markup — the caller
    rejects the upload (422 rejected_type) instead of storing it. Lowercases
    a decode-tolerant slice; a binary payload can only match by carrying the
    literal markup bytes, which is exactly what we are refusing."""
    probe = head[:256].decode("utf-8", errors="ignore").lower().lstrip()
    return any(probe.startswith(p) for p in _MARKUP_PROBES)


def sniff(filename: str, head: bytes) -> SniffResult:
    """Classify by bytes only — the declared extension is never consulted
    for the allowlist (a .png named .txt is still an image; sniff wins).
    Never raises."""
    if head.startswith(_JPEG):
        return SniffResult(FileKind.IMAGE, "image/jpeg")
    if head.startswith(_PNG):
        return SniffResult(FileKind.IMAGE, "image/png")
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return SniffResult(FileKind.IMAGE, "image/webp")
    if head.startswith(_GIF):
        return SniffResult(FileKind.IMAGE, "image/gif")
    if head.startswith(_PDF):
        return SniffResult(FileKind.PDF, "application/pdf")
    return SniffResult(FileKind.OTHER, "application/octet-stream")
