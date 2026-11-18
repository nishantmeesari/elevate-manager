import os
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.main import app


@pytest.fixture(scope="module")
def integration_context() -> tuple[TestClient, str]:
    database_url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.fail("TEST_DATABASE_URL or DATABASE_URL must target the isolated PostgreSQL test database")
    if urlparse(database_url).path.strip("/") != "elevate_test":
        pytest.fail("API integration tests must target the isolated elevate_test database")

    env = MonkeyPatch()
    env.setenv("DATABASE_URL", database_url)
    session_id = None
    customer_id = None
    booking_id = None

    try:
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        with psycopg.connect(database_url) as connection:
            for offset in range(1, 31):
                session_date = today + timedelta(days=offset)
                connection.execute(
                    "SELECT * FROM generate_class_sessions(%s, %s)",
                    (session_date, session_date),
                ).fetchall()
                session = connection.execute(
                    """
                    SELECT id
                    FROM sessions
                    WHERE status = 'scheduled'
                      AND (starts_at AT TIME ZONE 'Asia/Kolkata')::date = %s
                    ORDER BY starts_at, id
                    LIMIT 1
                    """,
                    (session_date,),
                ).fetchone()
                if session is not None:
                    session_id = session[0]
                    break

            if session_id is None:
                pytest.fail("No scheduled session was generated in the next 30 days")

            customer_id = connection.execute(
                """
                INSERT INTO customers (full_name, phone_e164)
                VALUES (%s, %s)
                RETURNING id
                """,
                ("API Read Test Customer", f"+919{uuid.uuid4().int % 10**10:010d}"),
            ).fetchone()[0]
            booking_id = connection.execute(
                """
                INSERT INTO bookings (booking_ref, session_id, customer_id, price_paise, source)
                VALUES (%s, %s, %s, 0, 'admin')
                RETURNING id
                """,
                (f"API-READ-{uuid.uuid4().hex}", session_id, customer_id),
            ).fetchone()[0]
            connection.commit()

        yield TestClient(app), str(session_id)
    finally:
        try:
            if booking_id is not None or customer_id is not None:
                with psycopg.connect(database_url) as cleanup_connection:
                    if booking_id is not None:
                        cleanup_connection.execute(
                            "DELETE FROM bookings WHERE id = %s",
                            (booking_id,),
                        )
                    if customer_id is not None:
                        cleanup_connection.execute(
                            "DELETE FROM customers WHERE id = %s",
                            (customer_id,),
                        )
                    cleanup_connection.commit()
        finally:
            env.undo()


def test_readiness_checks_database(integration_context: tuple[TestClient, str]) -> None:
    client, _ = integration_context

    response = client.get("/api/readiness")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_active_services_are_public_and_filtered(
    integration_context: tuple[TestClient, str],
) -> None:
    client, _ = integration_context

    response = client.get("/api/services")

    assert response.status_code == 200
    services = response.json()
    assert {service["name"] for service in services} >= {"Group Fitness", "Yoga"}
    assert all("phone_e164" not in service for service in services)


def test_active_trainers_include_active_services(
    integration_context: tuple[TestClient, str],
) -> None:
    client, _ = integration_context

    response = client.get("/api/trainers")

    assert response.status_code == 200
    trainers = {trainer["full_name"]: trainer for trainer in response.json()}
    assert {"Trainer A", "Trainer B"} <= trainers.keys()
    assert "Group Fitness" in trainers["Trainer A"]["services"]
    assert "Yoga" in trainers["Trainer B"]["services"]
    assert all("phone_e164" not in trainer for trainer in trainers.values())


def test_upcoming_sessions_report_occupancy(
    integration_context: tuple[TestClient, str],
) -> None:
    client, session_id = integration_context

    response = client.get("/api/sessions")

    assert response.status_code == 200
    session = next(item for item in response.json() if item["id"] == session_id)
    assert session["occupied_seats"] >= 1
    assert session["available_seats"] == session["capacity"] - session["occupied_seats"]


def test_session_availability_returns_public_capacity(
    integration_context: tuple[TestClient, str],
) -> None:
    client, session_id = integration_context

    response = client.get(f"/api/sessions/{session_id}/availability")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == session_id
    assert body["occupied_seats"] >= 1
    assert "customer_id" not in body


def test_missing_scheduled_session_returns_not_found(
    integration_context: tuple[TestClient, str],
) -> None:
    client, _ = integration_context

    response = client.get(f"/api/sessions/{uuid.uuid4()}/availability")

    assert response.status_code == 404
