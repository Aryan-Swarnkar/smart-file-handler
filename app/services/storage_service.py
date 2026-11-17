"""S3 storage via aioboto3 — the ONLY module that touches object storage.

Lifecycle: `start()` builds the client (lifespan startup), `close()` releases
it. Stateless per-request: presigned URLs are computed locally (no network),
so N API replicas sign independently — no shared state, horizontal-scale safe.

Local dev: SeaweedFS (S3 API compatible). Production: AWS S3 / Cloudflare R2
— same code, different S3_ENDPOINT_URL/creds from env.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.sniffing import sanitize_filename

if TYPE_CHECKING:
    # Service-specific stubs (types-aioboto3) give fully-typed S3 methods;
    # at runtime the client is duck-typed via aiobotocore.
    from types_aiobotocore_s3 import S3Client

logger = get_logger(__name__)

# 1 MiB read chunks: SHA-256 streaming + memory ceiling independent of the
# 25 MB object size (and of any future size bump).
READ_CHUNK_BYTES = 1024 * 1024


# SigV4 everywhere: presigned v4 URLs sign only the host header, so client
# Content-Type (which browsers set freely) can't invalidate the signature.
# SeaweedFS, real S3, and Cloudflare R2 all support v4; v2 is deprecated.
# AioConfig subclasses botocore.Config — same kwargs, plus aiohttp connector
# options. Passing a botocore Config *object* would land in connector_args.
_S3_CONFIG = AioConfig(signature_version="s3v4")


class StorageService:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._session: aioboto3.Session | None = None
        self._client_ctx: Any = None
        self._client: S3Client | None = None  # runtime: aiobotocore S3 client
        # Presign-only client bound to the PUBLIC endpoint: signatures embed
        # the host the client will connect to, so handed-out URLs must be
        # signed against what a host browser/curl can actually reach.
        self._presign_ctx: Any = None
        self._presign_client: S3Client | None = None

    async def start(self) -> None:
        s = self._settings
        self._session = aioboto3.Session(
            aws_access_key_id=s.S3_ACCESS_KEY_ID,
            aws_secret_access_key=s.S3_SECRET_ACCESS_KEY,
            region_name=s.S3_REGION,
        )
        self._client_ctx = self._session.client(
            "s3", endpoint_url=s.S3_ENDPOINT_URL, config=_S3_CONFIG
        )
        self._client = await self._client_ctx.__aenter__()
        public = s.s3_public_endpoint_url
        if public and public != s.S3_ENDPOINT_URL:
            self._presign_ctx = self._session.client(
                "s3", endpoint_url=public, config=_S3_CONFIG
            )
            self._presign_client = await self._presign_ctx.__aenter__()
        await self.ensure_bucket()
        logger.info(
            "storage_started",
            bucket=s.S3_BUCKET,
            endpoint=s.S3_ENDPOINT_URL,
            presign_endpoint=public,
        )

    async def close(self) -> None:
        if self._presign_ctx is not None:
            await self._presign_ctx.__aexit__(None, None, None)
            self._presign_client = None
            self._presign_ctx = None
        if self._client_ctx is not None:
            await self._client_ctx.__aexit__(None, None, None)
            self._client = None
            self._client_ctx = None

    def _bucket(self) -> str:
        return self._settings.S3_BUCKET

    def _require_client(self) -> S3Client:
        if self._client is None:
            raise RuntimeError("StorageService.start() has not run.")
        return self._client

    def _presigner(self) -> S3Client:
        # Falls back to the internal client when no public endpoint is set.
        client = self._presign_client or self._client
        if client is None:
            raise RuntimeError("StorageService.start() has not run.")
        return client

    async def ensure_bucket(self) -> None:
        """CreateBucket at startup. SeaweedFS auto-materializes it lazily, but
        an explicit PutBucket makes the flow identical on real S3 (where the
        bucket must exist before the first PutObject)."""
        client = self._require_client()
        try:
            await client.head_bucket(Bucket=self._bucket())
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("404", "NoSuchBucket"):
                raise
            try:
                await client.create_bucket(Bucket=self._bucket())
                logger.info("bucket_created", bucket=self._bucket())
            except ClientError as exc2:
                # Parallel startup (2 replicas) racing on the same bucket.
                if exc2.response.get("Error", {}).get("Code") not in (
                    "BucketAlreadyOwnedByYou",
                    "BucketAlreadyExists",
                ):
                    raise
        await self._ensure_cors()

    async def _ensure_cors(self) -> None:
        """Bucket CORS for direct browser PUTs (presigned upload flow).

        Without it SeaweedFS answers a browser's OPTIONS preflight with 403
        AccessDenied and the upload dies in the browser even though curl
        works. Origins come from CORS_ORIGINS — the same list that gates the
        API's CORS middleware, since a page allowed to call the API is also
        allowed to complete its uploads. Re-put only on change (idempotent;
        on real S3 this is normally managed by IaC instead).
        """
        origins = [
            o.strip() for o in self._settings.CORS_ORIGINS.split(",") if o.strip()
        ]
        if not origins:
            return
        # Any: the aiobotocore stub's CORSRule TypedDict shape, but comparing
        # against get_bucket_cors output needs plain dict semantics.
        rules: Any = [
            {
                "AllowedOrigins": origins,
                "AllowedMethods": ["GET", "PUT", "HEAD"],
                "AllowedHeaders": ["*"],
                "MaxAgeSeconds": 3000,
            }
        ]
        client = self._require_client()
        try:
            current = await client.get_bucket_cors(Bucket=self._bucket())
            if current.get("CORSRules") == rules:
                return
        except ClientError:
            pass  # NoSuchCORSConfiguration (or new bucket) → set below
        await client.put_bucket_cors(
            Bucket=self._bucket(), CORSConfiguration={"CORSRules": rules}
        )
        logger.info("bucket_cors_configured", origins=origins)

    async def ping(self) -> None:
        """Readiness check: HeadBucket. Raises on any failure."""
        await self._require_client().head_bucket(Bucket=self._bucket())

    async def presign_put(self, key: str, content_length: int) -> tuple[str, int]:
        """PUT url for the declared size.

        ContentLength rides in the request template AND the SigV4 signed
        headers: a PUT whose Content-Length differs from the declared size
        is rejected by storage with 403 (empirically enforced by
        SeaweedFS) — a lying client cannot even store. complete_upload
        keeps a defensive HEAD-vs-declared check (size_mismatch) for any
        object that reaches the key by other means.
        """
        client = self._presigner()
        expires = self._settings.PRESIGNED_URL_EXPIRE_SECONDS
        url = await client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self._bucket(),
                "Key": key,
                "ContentLength": content_length,
            },
            ExpiresIn=expires,
        )
        return url, expires

    async def presign_get(
        self,
        key: str,
        download: bool,
        filename: str | None = None,
        *,
        expires: int | None = None,
    ) -> tuple[str, int]:
        """GET url. download=True forces a save-as disposition carrying the
        SANITIZED original filename (never the raw user string, never the
        storage key — the key leaks the owner id, the name can carry header
        injection characters that sanitize_filename strips). `expires`
        overrides the TTL: the GET /files/{id} embed uses the spec's
        5-minute FILE_URL_EXPIRE_SECONDS, the /url endpoint keeps this
        default."""
        client = self._presigner()
        ttl = (
            expires if expires is not None else self._settings.PRESIGNED_URL_EXPIRE_SECONDS
        )
        params: dict[str, Any] = {"Bucket": self._bucket(), "Key": key}
        if download:
            safe = sanitize_filename(filename or key.rsplit("/", 1)[-1])
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        url = await client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=ttl
        )
        return url, ttl

    async def put_bytes(self, key: str, data: bytes) -> None:
        """Direct PUT (no presign) — a storage capability, not part of the
        client upload path: tests use it to plant objects the presigned
        flow refuses (e.g. a wrong-size body for the size_mismatch branch)."""
        client = self._require_client()
        await client.put_object(Bucket=self._bucket(), Key=key, Body=data)

    async def head(self, key: str) -> dict[str, Any] | None:
        """Object metadata or None if absent (404 → None, other errors raise)."""
        client = self._require_client()
        try:
            resp = await client.head_object(Bucket=self._bucket(), Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return resp

    async def read(self, key: str) -> AsyncGenerator[bytes, None]:
        """Stream the object body in 1 MiB chunks (hashing/sniffing)."""
        client = self._require_client()
        resp = await client.get_object(Bucket=self._bucket(), Key=key)
        try:
            async for chunk in resp["Body"]:
                yield chunk
        finally:
            resp["Body"].close()

    async def read_range(self, key: str, n: int) -> bytes:
        """First ≤n bytes via a ranged GET (Range header) — the sniff probe.

        A ranged GET stops the transfer at the server: a 25 MB object never
        leaves S3 just to sample its first 8 KiB. Objects smaller than n
        return whole (206 or 200). It replaced the v2 read_head helper
        (full-object GET + stream-abort), which streamed 25 MB to sample
        bytes and is now gone."""
        client = self._require_client()
        resp = await client.get_object(
            Bucket=self._bucket(), Key=key, Range=f"bytes=0-{n - 1}"
        )
        body = b""
        try:
            async for chunk in resp["Body"]:
                body += chunk
                if len(body) >= n:
                    break
        finally:
            resp["Body"].close()
        return body[:n]

    async def delete(self, key: str) -> None:
        """Idempotent (S3 delete of a missing key succeeds)."""
        await self._require_client().delete_object(Bucket=self._bucket(), Key=key)

    # Exceptions surfaced to callers that must map them:
    ClientError = ClientError
    BotoCoreError = BotoCoreError
