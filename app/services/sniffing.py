"""Content sniffing: decide a file's kind from its actual BYTES, not its name.

Matrix A (locked decision) is extension-driven for *processable* types, but
the user approved "sniff wins" for mismatches: a fake .jpg (really an .exe)
must never enter the face pipeline. Rules:

1. Image / PDF magics decide kind directly (jpg/png/webp/pdf).
2. ZIP magic (PK\\x03\\x04): docx/pptx/xlsx share ONE magic — the extension
   disambiguates the sub-kind; ZIP + non-office extension → other.
3. Text kinds (txt/md/csv) must decode as UTF-8 with no NUL bytes in the
   sampled head; binary junk named .txt → other.
4. Anything else (including a decodable file named .bat) → other.

Pure functions only — no I/O, no ML. The worker phases (P5/P6) re-validate
with real parsers; this layer only routes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.db.models.file import FileKind

# How many leading bytes to sample for sniffing (magic is ≤16; text probe
# wants a little more context than that).
SNIFF_SAMPLE_BYTES = 8192

TEXT_EXTENSIONS = frozenset({"txt", "md", "csv"})
OFFICE_EXTENSIONS = frozenset({"docx", "pptx", "xlsx"})

OFFICE_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

OCTET_STREAM = "application/octet-stream"

# JPEG SOI + APP0 marker; PNG signature; RIFF container with WEBP form type;
# PDF header; ZIP local-file-header (empty-archive/spanned variants too).
_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"
_PDF = b"%PDF-"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


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


def sniff(filename: str, head: bytes) -> SniffResult:
    """Classify by bytes first, extension second. Never raises."""
    ext = sanitize_extension(filename)

    # --- 1. unambiguous magics -------------------------------------------------
    if head.startswith(_JPEG):
        return SniffResult(FileKind.IMAGE, "image/jpeg")
    if head.startswith(_PNG):
        return SniffResult(FileKind.IMAGE, "image/png")
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return SniffResult(FileKind.IMAGE, "image/webp")
    if head.startswith(_PDF):
        return SniffResult(FileKind.PDF, "application/pdf")

    # --- 2. ZIP family: magic proves container, extension proves dialect ------
    if head.startswith(_ZIP_MAGICS):
        if ext in OFFICE_EXTENSIONS:
            return SniffResult(FileKind.DOCUMENT, OFFICE_MIME[ext])
        return SniffResult(FileKind.OTHER, "application/zip")

    # --- 3. text kinds: decodable UTF-8 sample, no NUL bytes -------------------
    if ext in TEXT_EXTENSIONS:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return SniffResult(FileKind.OTHER, OCTET_STREAM)
        if b"\x00" in head:
            return SniffResult(FileKind.OTHER, OCTET_STREAM)
        mime = {
            "txt": "text/plain",
            "md": "text/markdown",
            "csv": "text/csv",
        }[ext]
        return SniffResult(FileKind.TEXT, mime)

    # --- 4. everything else ----------------------------------------------------
    # Includes: extension claims image/pdf/office but bytes disagree (sniff
    # wins → other), plain .zip, .bat, unknown binaries, empty files.
    return SniffResult(FileKind.OTHER, OCTET_STREAM)
