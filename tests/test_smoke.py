"""P0 smoke tests: app boots, endpoints respond, error envelope is uniform."""

from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_ok(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert "x-request-id" in resp.headers


async def test_root_index(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "smart-file-handler"
    assert body["docs"] == "/docs"


async def test_openapi_available(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/healthz" in resp.json()["paths"]


async def test_unknown_route_returns_envelope(client: AsyncClient) -> None:
    resp = await client.get("/definitely/not/here")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "http_error"
    assert body["request_id"] == resp.headers["x-request-id"]


async def test_readyz_reports_redis_down(client: AsyncClient) -> None:
    # conftest points REDIS_URL at a closed port, so readiness must be 503
    # and name the failing dependency.
    resp = await client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["redis"] == "down"


async def test_readyz_accepts_incoming_request_id(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"x-request-id": "test-req-42"})
    assert resp.headers["x-request-id"] == "test-req-42"
