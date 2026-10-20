from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

# Set a test-only key before importing the application/settings modules.
os.environ.setdefault(
    "SECRET_KEY",
    "test-only-secret-key-do-not-use-in-production-0123456789",
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models  # noqa: F401 — ensures ORM models are registered in Base.metadata.
import routes as routes_module
from database import Base, get_db
from main import app

try:
    import services as services_module
except ImportError:
    services_module = None


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    """Provide an isolated SQLite database and temporary upload directory."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    Base.metadata.create_all(bind=engine)

    def override_get_db():
        with TestingSessionLocal() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db

    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(routes_module, "BASE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(routes_module, "UPLOAD_DIR", upload_dir, raising=False)

    # Also patch these if routes.py delegates storage work to services.py.
    if services_module is not None:
        monkeypatch.setattr(services_module, "BASE_DIR", tmp_path, raising=False)
        monkeypatch.setattr(services_module, "UPLOAD_DIR", upload_dir, raising=False)

    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()