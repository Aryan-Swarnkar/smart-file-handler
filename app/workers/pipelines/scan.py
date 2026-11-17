"""ClamAV scan pipeline (clamd INSTREAM protocol, zero extra dependencies).

The protocol is three steps over one TCP connection: send the null-terminated
command `zINSTREAM`, then length-prefixed chunks (<4-byte big-endian len>
<bytes>), terminated by a zero-length chunk; clamd answers with one line —
`stream: OK`, `stream: <signature> FOUND`, or an ERROR line — followed by NUL.

Decisions this module pins:
- Unreachable clamd / connection trouble -> ScanUnavailable (a RETRYABLE
  error): the task retries and the file is never marked clean on a maybe —
  the spec's "if clamd is unreachable, retry (don't mark clean)".
- An ERROR response (e.g. the scanner's stream size limit exceeded) is
  deterministic -> NonRetryableError with a display-safe reason.
- ML_STUB=1 swaps the network client for a deterministic fake: infected iff
  the bytes contain the EICAR test signature (legal and expected for AV
  testing), clean otherwise. This makes the quarantine path testable in CI
  without clamd; the REAL clamd path is proven by the live E2E (worker ->
  clamd inside the compose network — clamd deliberately has no host port).
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from pathlib import Path
from typing import NamedTuple

from app.core.config import get_settings
from app.core.logging import get_logger
from app.workers.errors import NonRetryableError

logger = get_logger(__name__)

# ClamAV INSTREAM framing: 64 KiB chunks keep memory flat for 25 MB objects.
CHUNK_SIZE = 65536
_COMMAND = b"zINSTREAM\0"

# The standard AV test string (EICAR) — legal and expected for scan testing.
EICAR_SIGNATURE = (
    "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


class ScanUnavailable(Exception):
    """clamd could not be reached / answered uncleanly — RETRYABLE."""


class ScanVerdict(NamedTuple):
    infected: bool
    signature: str | None


async def scan_file(path: Path) -> ScanVerdict:
    """One file -> verdict. Never returns 'clean' on a maybe."""
    if get_settings().ML_STUB:
        data = await asyncio.to_thread(path.read_bytes)  # rule 8: no blocking
        return _stub_verdict(data)
    return await _clamd_instream(path)


def _stub_verdict(data: bytes) -> ScanVerdict:
    """Deterministic stub: EICAR bytes -> infected, everything else clean."""
    if EICAR_SIGNATURE.encode("ascii", errors="ignore") in data:
        return ScanVerdict(infected=True, signature="Eicar-Test-Signature")
    return ScanVerdict(infected=False, signature=None)


def _parse_verdict(response: bytes) -> ScanVerdict:
    """`stream: OK` / `stream: <sig> FOUND` / anything else -> error."""
    line = response.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
    if line == "stream: OK":
        return ScanVerdict(infected=False, signature=None)
    if line.startswith("stream: ") and line.endswith(" FOUND"):
        return ScanVerdict(infected=True, signature=line[len("stream: "):-len(" FOUND")])
    raise ScanUnavailable(f"clamd answered uncleanly: {line[:120]}")


async def _clamd_instream(path: Path) -> ScanVerdict:
    """Stream the file to clamd via INSTREAM; one connection, one verdict."""
    settings = get_settings()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.CLAMAV_HOST, settings.CLAMAV_PORT),
            timeout=settings.CLAMAV_CONNECT_TIMEOUT,
        )
    except (OSError, TimeoutError) as exc:
        raise ScanUnavailable(
            f"clamd unreachable ({type(exc).__name__})"
        ) from exc

    loop = asyncio.get_running_loop()
    try:
        writer.write(_COMMAND)
        with path.open("rb") as fh:
            while True:
                chunk = await loop.run_in_executor(None, fh.read, CHUNK_SIZE)
                if not chunk:
                    break
                writer.write(struct.pack("!L", len(chunk)) + chunk)
        writer.write(struct.pack("!L", 0))  # terminating zero-length chunk
        await writer.drain()

        response = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=settings.CLAMAV_TIMEOUT)
        verdict = _parse_verdict(response)
    except TimeoutError as exc:
        raise ScanUnavailable("clamd timed out mid-stream") from exc
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise ScanUnavailable(f"clamd stream failed ({type(exc).__name__})") from exc
    finally:
        writer.close()
        with contextlib.suppress(OSError, ConnectionResetError):
            await writer.wait_closed()

    if verdict.infected:
        logger.warning(
            "scan_infected", signature=verdict.signature, path=path.name
        )
    return verdict


def assert_scannable_size(size_bytes: int) -> None:
    """Guard BEFORE streaming: clamd's INSTREAM limit is a hard server cap;
    an oversized object is deterministically unscannable."""
    limit = get_settings().CLAMAV_STREAM_MAX_BYTES
    if size_bytes > limit:
        raise NonRetryableError(
            f"scan: object exceeds the scanner's stream limit ({limit} bytes)"
        )
