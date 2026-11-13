# H — Object storage (S3) ✅ P3

**Core skill**: storing files durably outside the database, and letting
clients upload/download without proxying bytes through the API.

## What this codebase does

| Aspect | Where | Choice |
|---|---|---|
| The ONLY S3 module | `app/services/storage.py` | `StorageService` — every other module calls it; boto3 is never imported elsewhere |
| Client library | `pyproject.toml` | **aioboto3** (async botocore) — no thread pools, no blocking calls in routes |
| Signature scheme | `app/services/storage.py` | **SigV4 only** (`AioConfig(signature_version="s3v4")`) — v2 signs the request's Content-Type, so any browser/curl PUT with a Content-Type header fails validation; v4 presigned URLs sign only the host |
| Bucket bootstrap | `StorageService.ensure_bucket()` | HeadBucket → CreateBucket; the 2-replica race is handled (`BucketAlreadyOwnedByYou/Exists`) |
| CORS bootstrap | `StorageService._ensure_cors()` | PutBucketCors from `CORS_ORIGINS` — SeaweedFS 403s anonymous OPTIONS preflights otherwise (verified live); re-put only when rules change |
| Presigned PUT | `presign_put()` | URL-only upload: client → S3 directly; API never sees the bytes. Signed against the **public** endpoint |
| Presigned GET | `presign_get()` | Opaque, expiring view URLs; `download=true` → `ResponseContentDisposition: attachment` |
| Dual endpoint | `app/core/config.py` | `S3_ENDPOINT_URL` (internal ops, e.g. `s3:8333`) vs `S3_PUBLIC_ENDPOINT_URL` (handed-out URLs, e.g. `localhost:9000`) — **presigned URLs embed the host they were signed for; it must be one the client can reach** |
| Streaming read | `read()` / `read_head()` | 1 MiB chunks (`READ_CHUNK_BYTES`), generator + deterministic `aclose()` — constant memory |
| Health | `storage.ping()` | HeadBucket in `/readyz` (`app/api/routers/health.py`) |

## Why presigned URLs (architecture decision)

1. The API stays **stateless and thin** — N replicas can serve N uploads with
   zero byte-proxying; uploads scale with S3, not with API pod count.
2. The signature IS the authorization: no public buckets, no ACLs, every URL
   expires (`PRESIGNED_URL_EXPIRE_SECONDS`, 900s default).
3. The client declares `size` at init; `/complete` re-verifies it with a HEAD
   (`size_mismatch` 422) because presigned v4 PUTs do not enforce length.

## Why the two-endpoint split bit us (real bug, caught by live testing)

In containers the API signed URLs for `http://s3:8333` — a hostname only
resolvable INSIDE the compose network. Tests passed (app ran in-process on
the host, where `localhost:9000` was configured); real host clients got a
dead URL. Fix: presign against the public endpoint. This is exactly the
"behind-a-proxy/reverse-proxy" problem in production (AWS: `--endpoint-url`
vs public bucket URL; SeaweedFS has `-s3.externalUrl` for the same reason).

## Backends (same code path)

| Environment | Backend | Notes |
|---|---|---|
| Local dev | SeaweedFS `chrislusf/seaweedfs:3.99` | S3 API compatible; MinIO community discontinued (verified) |
| Production | AWS S3 / Cloudflare R2 | R2 requires SigV4 — already our default |

## Key layout

`{user_id}/{file_id}.{ext}` (`app/services/files.py`): uuid v4 per file —
no filename collisions, no path traversal, and **per-user prefix** makes
future lifecycle rules (per-user retention) trivial. Rename changes only
the DB display name; the key is immutable.

## Tests proving it

`tests/test_files.py` — real SeaweedFS round-trips: presigned PUT 200,
HEAD-vs-declared size, streaming SHA-256 equals the client-side hash, dedup
deletes the duplicate object, delete removes the object (presigned GET
afterwards ≠ 200), presign uses the public endpoint (`test_presigned_urls_use_public_endpoint`).
`scripts/p3_live_check.ps1` — 29 live checks including a browser-style CORS
preflight.
