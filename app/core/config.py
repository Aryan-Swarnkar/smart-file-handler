from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator


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
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ALGORITHM: str = "HS256"
    


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

@model_validator(mode="after")
def validate_secret_key(self):
    allowed_dev_envs = {"test", "testing", "localhost", "dev", "development"}
    
    placeholders = {
        "secret",
        "changeme",
        "change-me",
        "your-secret-key",
        "your-secret-key-here",
        "replace-me",
        "placeholder",
    }
    
    if self.ENV.strip().lower() not in allowed_dev_envs:
        if len(self.SECRET_KEY < 30):
            raise ValueError("SECRET_KEY must be atleast 32 characters in non-development modes")

        if self.SECRET_KEY.strip().lower() in placeholders:
            raise ValueError("SECRET_KEY cannot be a placeholder in non-development environments.")
        
    return self
