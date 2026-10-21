from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    DATABASE_URL: str
    STORAGE_ROOT: Path = BASE_DIR / "storage"
    MAX_UPLOAD_MB: int = 10
    SECRET_KEY: str
    ENV: str = "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()