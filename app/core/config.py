"""Application configuration.

Everything comes from environment variables (12-factor). `.env` is supported
for local development only; in production env vars win anyway (pydantic-settings
gives process env precedence over dotenv values).

NOTE: this file deliberately fixes two bugs found in the previous stage-1
implementation: the env validator now lives INSIDE the Settings class (a
module-level @model_validator never ran), and the length check no longer
compares a string to an int before calling len().
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> project root
BASE_DIR = Path(__file__).resolve().parents[2]

SECRET_KEY_MIN_LENGTH = 32
_SECRET_KEY_PLACEHOLDERS = frozenset(
    {
        "secret",
        "change-me",
        "changeme",
        "your-secret-key",
        "your-secret-key-here",
        "replace-me",
        "placeholder",
        "test",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # "ignore" so compose-provided POSTGRES_* vars don't crash the app.
        extra="ignore",
    )

    # --- core ---------------------------------------------------------------
    ENV: Literal["development", "test", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"

    # --- auth ----------------------------------------------------------------
    SECRET_KEY: str
    JWT_ALGORITHM: Literal["HS256"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # --- rate limiting (auth endpoints; atomic Redis Lua token bucket) --------
    # Capacity = knob per minute, refill = knob/60 per second (burst-capable,
    # same average). Keyed by IP + sha256(email) (Phase 3). Namespace isolates
    # key spaces (multi-replica shares Redis; tests get a unique per-run
    # namespace so counters never leak between runs).
    RATELIMIT_NAMESPACE: str = "rl"
    LOGIN_RATELIMIT_PER_MINUTE: int = 10
    REGISTER_RATELIMIT_PER_MINUTE: int = 5
    REFRESH_RATELIMIT_PER_MINUTE: int = 30

    # --- infrastructure ------------------------------------------------------
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- object storage (validated for real use in P3) ------------------------
    # S3_ENDPOINT_URL: where THIS app talks to S3 (inside the network).
    # S3_PUBLIC_ENDPOINT_URL: where UPLOAD CLIENTS must reach S3 — presigned
    # URLs are built from it. Defaults to S3_ENDPOINT_URL when empty. They
    # differ in containers: api signs with s3:8333 internally but hands out
    # http://localhost:9000 so a host browser/curl can complete the PUT.
    S3_ENDPOINT_URL: str | None = None
    S3_PUBLIC_ENDPOINT_URL: str = ""
    S3_REGION: str = "us-east-1"
    S3_BUCKET: str = "smart-file-handler"
    S3_ACCESS_KEY_ID: str | None = None
    S3_SECRET_ACCESS_KEY: str | None = None
    PRESIGNED_URL_EXPIRE_SECONDS: int = 900

    @property
    def s3_public_endpoint_url(self) -> str:
        return self.S3_PUBLIC_ENDPOINT_URL or (self.S3_ENDPOINT_URL or "")

    # --- uploads ---------------------------------------------------------------
    MAX_UPLOAD_MB: int = 25
    # pending_upload window: init reserves quota + hands out a presigned PUT;
    # past this stamp the upload is abandoned (the Phase 6 sweeper reaps the
    # row and releases the reservation).
    UPLOAD_TTL_SECONDS: int = 900
    # The spec's 5-minute presigned GET on GET /files/{id} (separate from the
    # 15-min PUT window above).
    FILE_URL_EXPIRE_SECONDS: int = 300
    # Idempotency-Key replay TTL: retries past it are simply a new upload.
    IDEMPOTENCY_TTL_DAYS: int = 7

    # --- quotas (Phase 5 enforces; set at registration from this knob) --------
    DEFAULT_STORAGE_QUOTA_GB: int = 5

    @property
    def default_storage_quota_bytes(self) -> int:
        return self.DEFAULT_STORAGE_QUOTA_GB * 1024 * 1024 * 1024

    # --- Phase 6 pipeline: relay + Celery + sweeper -----------------------------
    # Relay: one claim cycle per poll; RELAY_BATCH_SIZE rows per claim. No
    # attempts cap on purpose: the only realistic send failure is broker
    # downtime, and a cap would strand every unpublished row after a long
    # outage — per-row send isolation plus escalating log severity instead.
    RELAY_POLL_SECONDS: float = 2.0
    RELAY_BATCH_SIZE: int = 20
    # Task limits: soft (raises inside the task, retryable) < hard (kills the
    # worker process; acks_late + reject_on_worker_lost turn that into a
    # redelivery) < broker visibility timeout (a running task must never be
    # redelivered mid-flight).
    TASK_SOFT_TIME_LIMIT: int = 90
    TASK_HARD_TIME_LIMIT: int = 120
    BROKER_VISIBILITY_TIMEOUT: int = 300
    # Retryable failures: exponential backoff + jitter; the LAST attempt
    # writes files.status='failed' + files.error (the reason), otherwise the
    # row would loop through the sweeper's rescue forever.
    CELERY_TASK_MAX_RETRIES: int = 5
    # Sweeper cadence + thresholds. Every expiry/stuck comparison runs in SQL
    # against now() — host clock skew never decides lifecycle.
    SWEEP_INTERVAL_SECONDS: int = 60
    STUCK_FILE_MINUTES: int = 10
    PENDING_SWEEP_GRACE_SECONDS: int = 60

    # --- ClamAV (scan pipeline, Phase 7) -----------------------------------------
    CLAMAV_HOST: str = "localhost"
    CLAMAV_PORT: int = 3310

    # --- ML pipeline (Phase 7) -----------------------------------------------
    # ML_STUB=1: workers use fixed-delay fakes returning deterministic labels
    # instead of real models. Real models only on explicit ask, never alongside
    # the full stack + Locust (16 GB host).
    ML_STUB: bool = True

    # --- HTTP -------------------------------------------------------------------
    # Comma-separated origins; empty string disables CORS middleware.
    CORS_ORIGINS: str = ""

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @model_validator(mode="after")
    def _validate_secrets(self) -> Settings:
        """Reject weak secrets in production. Dev/test stay frictionless."""
        if self.is_production:
            if len(self.SECRET_KEY) < SECRET_KEY_MIN_LENGTH:
                raise ValueError(
                    f"SECRET_KEY must be at least {SECRET_KEY_MIN_LENGTH} "
                    "characters in production"
                )
            if self.SECRET_KEY.strip().lower() in _SECRET_KEY_PLACEHOLDERS:
                raise ValueError("SECRET_KEY cannot be a placeholder in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Tests clear the cache when they mutate env vars."""
    return Settings()  # type: ignore[call-arg]  # populated from env
