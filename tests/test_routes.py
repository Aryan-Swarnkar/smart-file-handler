from __future__ import annotations

from fastapi.testclient import TestClient

import routes as routes_module


PASSWORD = "test-password-123"


def register_and_login(
    client: TestClient,
    email: str = "alice@example.com",
    name: str = "Alice",
) -> dict[str, str]:
    registration = client.post(
        "/auth/register",
        json={"name": name, "email": email, "password": PASSWORD},
    )
    assert registration.status_code == 201, registration.text

    login = client.post(
        "/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text

    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def upload_pdf(client: TestClient, headers: dict[str, str], content: bytes = b"sample PDF content"):
    return client.post(
        "/files",
        headers=headers,
        files={"upload": ("notes.pdf", content, "application/pdf")},
    )


def test_health_and_home_routes(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    home = client.get("/")
    assert home.status_code == 200
    assert home.json()["message"] == "Welcome to Smart File Handler"
    assert home.json()["docs"] == "/docs"


def test_register_returns_user_without_password_hash(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json={
            "name": "Alice",
            "email": "Alice@example.com",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Alice"
    assert body["email"] == "alice@example.com"
    assert body["is_active"] is True
    assert "id" in body
    assert "created_at" in body
    assert "updated_at" in body
    assert "password" not in body
    assert "password_hash" not in body


def test_register_rejects_duplicate_email(client: TestClient) -> None:
    payload = {"name": "Alice", "email": "alice@example.com", "password": PASSWORD}
    first = client.post("/auth/register", json=payload)
    second = client.post("/auth/register", json=payload)

    assert first.status_code == 201
    assert second.status_code == 409


def test_register_validates_email_and_password(client: TestClient) -> None:
    bad_email = client.post(
        "/auth/register",
        json={"name": "Alice", "email": "not-an-email", "password": PASSWORD},
    )
    short_password = client.post(
        "/auth/register",
        json={"name": "Alice", "email": "alice@example.com", "password": "short"},
    )

    assert bad_email.status_code == 422
    assert short_password.status_code == 422


def test_login_rejects_wrong_password(client: TestClient) -> None:
    client.post(
        "/auth/register",
        json={"name": "Alice", "email": "alice@example.com", "password": PASSWORD},
    )

    response = client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "wrong-password"},
    )
    assert response.status_code == 401


def test_login_returns_bearer_token(client: TestClient) -> None:
    login_headers = register_and_login(client)
    token = login_headers["Authorization"].removeprefix("Bearer ")

    assert token
    files_response = client.get("/files", headers=login_headers)
    assert files_response.status_code == 200


def test_file_routes_require_authentication(client: TestClient) -> None:
    assert client.get("/files").status_code == 401
    assert client.post(
        "/files",
        files={"upload": ("notes.pdf", b"content", "application/pdf")},
    ).status_code == 401
    assert client.get("/files/1").status_code == 401
    assert client.patch("/files/1", json={"file_name": "new.pdf"}).status_code == 401
    assert client.delete("/files/1").status_code == 401
    assert client.get("/files/1/download").status_code == 401


def test_file_crud_upload_list_read_update_download_delete(client: TestClient) -> None:
    headers = register_and_login(client)

    upload = upload_pdf(client, headers)
    assert upload.status_code == 201, upload.text
    created = upload.json()
    file_id = created["id"]

    assert created["file_name"] == "notes.pdf"
    assert created["file_type"] == "document"
    assert created["file_extension"] == ".pdf"
    assert created["mime_type"] == "application/pdf"
    assert created["category"] is None
    assert created["processing_status"] == "pending"
    assert "file_path" not in created  # Internal storage path is not exposed.

    file_list = client.get("/files", headers=headers)
    assert file_list.status_code == 200
    assert len(file_list.json()) == 1
    assert file_list.json()[0]["id"] == file_id

    read = client.get(f"/files/{file_id}", headers=headers)
    assert read.status_code == 200
    assert read.json()["file_name"] == "notes.pdf"

    update = client.patch(
        f"/files/{file_id}",
        headers=headers,
        json={"file_name": "study_notes.pdf", "category": "study"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["file_name"] == "study_notes.pdf"
    assert update.json()["category"] == "study"

    download = client.get(f"/files/{file_id}/download", headers=headers)
    assert download.status_code == 200, download.text
    assert download.content == b"sample PDF content"
    assert "study_notes.pdf" in download.headers.get("content-disposition", "")

    delete = client.delete(f"/files/{file_id}", headers=headers)
    assert delete.status_code == 204

    assert client.get("/files", headers=headers).json() == []
    assert client.get(f"/files/{file_id}", headers=headers).status_code == 404
    assert not any(path.is_file() for path in (routes_module.BASE_DIR / "uploads").rglob("*"))


def test_users_cannot_access_each_others_files(client: TestClient) -> None:
    alice_headers = register_and_login(client, "alice@example.com", "Alice")
    upload = upload_pdf(client, alice_headers)
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]

    bob_headers = register_and_login(client, "bob@example.com", "Bob")

    assert client.get("/files", headers=bob_headers).json() == []
    assert client.get(f"/files/{file_id}", headers=bob_headers).status_code == 404
    assert client.get(f"/files/{file_id}/download", headers=bob_headers).status_code == 404
    assert client.patch(
        f"/files/{file_id}",
        headers=bob_headers,
        json={"file_name": "stolen.pdf"},
    ).status_code == 404
    assert client.delete(f"/files/{file_id}", headers=bob_headers).status_code == 404

    # The other user's failed operations must not affect the owner's file.
    assert client.get(f"/files/{file_id}", headers=alice_headers).status_code == 200


def test_patch_requires_at_least_one_field(client: TestClient) -> None:
    headers = register_and_login(client)
    upload = upload_pdf(client, headers)
    file_id = upload.json()["id"]

    response = client.patch(f"/files/{file_id}", headers=headers, json={})
    assert response.status_code == 400


def test_invalid_token_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/files",
        headers={"Authorization": "Bearer not-a-valid-jwt"},
    )
    assert response.status_code == 401


def test_oversized_upload_is_rejected(client: TestClient, monkeypatch) -> None:
    headers = register_and_login(client)

    # Keep the test small; no need to generate an actual 25 MB upload.
    monkeypatch.setattr(routes_module, "MAX_UPLOAD_SIZE", 5, raising=False)
    try:
        import services as services_module
    except ImportError:
        services_module = None
    if services_module is not None:
        monkeypatch.setattr(services_module.settings, "MAX_UPLOAD_SIZE", 5, raising=False)

    response = client.post(
        "/files",
        headers=headers,
        files={"upload": ("large.pdf", b"this is larger than five bytes", "application/pdf")},
    )

    assert response.status_code == 413, response.text
    assert client.get("/files", headers=headers).json() == []
    assert not any(path.is_file() for path in (routes_module.BASE_DIR / "uploads").rglob("*"))


def test_unknown_file_returns_404(client: TestClient) -> None:
    headers = register_and_login(client)
    response = client.get("/files/999999", headers=headers)
    assert response.status_code == 404
