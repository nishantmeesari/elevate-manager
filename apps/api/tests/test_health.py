from fastapi.testclient import TestClient
from app.db import _database_connect_kwargs

from app.main import app, _cors_origins


client = TestClient(app)


def test_health_check() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_check_allows_configured_cors_origin() -> None:
    response = client.get(
        "/health",
        headers={"Origin": "http://localhost:5173"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_hosted_database_defaults_to_required_ssl(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_SSLMODE", raising=False)

    assert _database_connect_kwargs(
        "postgresql://user:password@db.example.test:5432/elevate_manager"
    ) == {"sslmode": "require"}


def test_local_database_does_not_force_ssl(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_SSLMODE", raising=False)

    assert _database_connect_kwargs(
        "postgresql://postgres@127.0.0.1:5432/elevate_manager"
    ) == {}


def test_database_sslmode_can_be_overridden(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_SSLMODE", "verify-full")

    assert _database_connect_kwargs(
        "postgresql://user:password@db.example.test:5432/elevate_manager"
    ) == {"sslmode": "verify-full"}


def test_cors_origin_list_is_explicit(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "https://staging.example.test, https://admin.example.test/",
    )

    assert _cors_origins() == [
        "https://staging.example.test",
        "https://admin.example.test",
    ]


def test_wildcard_cors_origin_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")

    try:
        _cors_origins()
    except RuntimeError as error:
        assert str(error) == "Wildcard CORS origins are not allowed"
    else:
        raise AssertionError("Wildcard CORS origin was accepted")
