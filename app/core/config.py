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

    # --- rate limiting (auth endpoints; per client IP, fixed window) -----------
    # Namespace isolates key spaces (multi-replica shares Redis; tests get a
    # unique per-run namespace so rate-limit counters never leak between runs).
    RATELIMIT_NAMESPACE: str = "rl"
    LOGIN_RATELIMIT_PER_MINUTE: int = 10
    REGISTER_RATELIMIT_PER_MINUTE: int = 5

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

    # --- queue / worker (P4) -----------------------------------------------------
    # The API enqueues a job right after complete commits; the worker-side sweep
    # is the crash-recovery net (API died mid-enqueue, Redis was down, or the
    # enqueue failed). Orphan init rows (bytes never PUT, presigned URL long
    # expired) are deleted by the same sweep after a grace window.
    SWEEP_INTERVAL_SECONDS: int = 30
    ORPHAN_INIT_MINUTES: int = 60
    WORKER_MAX_TRIES: int = 3
    WORKER_RETRY_BASE_SECONDS: int = 5
    WORKER_RETRY_MAX_SECONDS: int = 300

    # --- HTTP -------------------------------------------------------------------
    # Comma-separated origins; empty string disables CORS middleware.
    CORS_ORIGINS: str = ""

    # --- observability ------------------------------------------------------------
    SENTRY_DSN: str | None = None

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
