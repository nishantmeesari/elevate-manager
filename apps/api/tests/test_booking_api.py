import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from threading import Barrier
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import conninfo_to_dict
import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.main import app
from app.security import Principal, create_access_token, hash_password


IST = ZoneInfo("Asia/Kolkata")
DATABASE_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_FILES = [
    DATABASE_ROOT / "db" / "migrations" / filename
    for filename in (
        "001_initial.sql",
        "002_passes_and_policies.sql",
        "003_holds_waitlist_quiet_hours.sql",
        "004_studio_payment_and_timetable.sql",
        "005_quiet_hours_all_notifications.sql",
        "006_application_auth_and_payments.sql",
    )
]
SEED_FILE = DATABASE_ROOT / "db" / "seed.sql"
LOCAL_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validated_test_database_parameters(database_url: str) -> dict[str, str]:
    parameters = conninfo_to_dict(database_url)
    if parameters.get("dbname") != "elevate_test":
        raise ValueError("TEST_DATABASE_URL must target database elevate_test")
    if parameters.get("hostaddr"):
        raise ValueError("TEST_DATABASE_URL must not specify hostaddr")
    if parameters.get("service"):
        raise ValueError("TEST_DATABASE_URL must not specify service")
    if parameters.get("host") not in LOCAL_DATABASE_HOSTS:
        raise ValueError(
            "TEST_DATABASE_URL must specify host localhost, 127.0.0.1, or ::1"
        )
    return parameters


def _rebuild_test_database(database_url: str) -> None:
    test_parameters = _validated_test_database_parameters(database_url)
    admin_parameters = test_parameters.copy()
    admin_parameters["dbname"] = "postgres"
    with psycopg.connect(**admin_parameters, autocommit=True) as connection:
        connection.execute("DROP DATABASE IF EXISTS elevate_test WITH (FORCE)")
        connection.execute("CREATE DATABASE elevate_test")

    with psycopg.connect(**test_parameters, autocommit=True) as connection:
        for sql_file in MIGRATION_FILES + [SEED_FILE]:
            connection.execute(sql_file.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "conninfo, expected_host",
    [
        ("host=localhost dbname=elevate_test", "localhost"),
        ("host=127.0.0.1 dbname=elevate_test", "127.0.0.1"),
        ("host=::1 dbname=elevate_test", "::1"),
    ],
)
def test_validated_test_database_parameters_accept_local_hosts(
    conninfo: str,
    expected_host: str,
) -> None:
    parameters = _validated_test_database_parameters(conninfo)

    assert parameters["dbname"] == "elevate_test"
    assert parameters["host"] == expected_host


@pytest.mark.parametrize(
    "conninfo",
    [
        "host=localhost dbname=postgres",
        "host=db.example.com dbname=elevate_test",
        "dbname=elevate_test",
        "host=localhost hostaddr=203.0.113.1 dbname=elevate_test",
        "host=localhost service=remote_test dbname=elevate_test",
    ],
)
def test_validated_test_database_parameters_reject_unsafe_targets(conninfo: str) -> None:
    with pytest.raises(ValueError):
        _validated_test_database_parameters(conninfo)


def test_private_customer_endpoint_requires_authentication() -> None:
    with TestClient(app) as client:
        response = client.get("/api/me")

    assert response.status_code == 401


@pytest.fixture(scope="module")
def booking_context() -> tuple[TestClient, dict[str, list[str]]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.fail("TEST_DATABASE_URL must target the isolated PostgreSQL test database")
    _validated_test_database_parameters(database_url)

    env = MonkeyPatch()
    env.setenv("DATABASE_URL", database_url)
    env.setenv("AUTH_SECRET", "integration-test-auth-secret")
    env.setenv("INITIAL_ADMIN_BOOTSTRAP_TOKEN", "integration-bootstrap-token")
    env.setenv("ALLOW_LOCAL_TEST_PAYMENTS", "true")
    created = {
        "bookings": [],
        "customers": [],
        "passes": [],
        "sessions": [],
    }

    try:
        _rebuild_test_database(database_url)
        client = TestClient(app)
        original_post = client.post

        def authenticated_post(url: str, *args, **kwargs):
            payload = kwargs.get("json")
            if url.startswith("/api/bookings") and isinstance(payload, dict):
                customer_id = payload.get("customer_id")
                if customer_id is not None:
                    headers = dict(kwargs.get("headers") or {})
                    headers.setdefault("Authorization", _customer_headers(customer_id)["Authorization"])
                    kwargs["headers"] = headers
            return original_post(url, *args, **kwargs)

        client.post = authenticated_post
        yield client, created
    finally:
        try:
            _rebuild_test_database(database_url)
        finally:
            env.undo()


def test_customer_registration_and_profile_isolation(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, _ = booking_context
    phone = f"+919{uuid.uuid4().int % 10**10:010d}"
    response = client.post(
        "/api/auth/register",
        json={
            "full_name": "Registered Customer",
            "phone_e164": phone,
            "email": f"{uuid.uuid4().hex}@example.test",
            "password": "IntegrationTestPassword123!",
        },
    )

    assert response.status_code == 201
    profile = client.get(
        "/api/me",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert profile.status_code == 200
    assert profile.json()["phone_e164"] == phone


def test_customer_cannot_use_another_customer_identity(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    owner_id, session_id, pass_id = _setup(created)
    other_id, _, _ = _setup(created)

    response = _post_as_customer(
        client,
        "/api/bookings",
        owner_id,
        json=_booking_request(other_id, session_id, pass_id),
    )

    assert response.status_code == 403


def test_bootstrapped_owner_can_access_admin_dashboard(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, _ = booking_context
    response = client.post(
        "/api/auth/bootstrap-admin",
        json={
            "bootstrap_token": "integration-bootstrap-token",
            "email": f"{uuid.uuid4().hex}@admin.example.test",
            "full_name": "Integration Owner",
            "password": "IntegrationAdminPassword123!",
        },
    )

    assert response.status_code == 201
    dashboard = client.get(
        "/api/admin/dashboard",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert dashboard.status_code == 200


def test_existing_customer_token_is_rejected_after_deactivation(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, _, _ = _setup(created)
    headers = _customer_headers(customer_id)

    assert client.get("/api/me", headers=headers).status_code == 200
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "UPDATE customers SET anonymized_at = statement_timestamp() WHERE id = %s",
            (customer_id,),
        )
        connection.commit()

    response = client.get("/api/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "Account is inactive or unavailable"


def test_existing_staff_token_uses_current_role_after_role_change(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, _ = booking_context
    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as connection:
        staff_id = connection.execute(
            """
            INSERT INTO users (email, password_hash, full_name, role)
            VALUES (%s, %s, %s, 'receptionist')
            RETURNING id
            """,
            (
                f"{uuid.uuid4().hex}@staff.example.test",
                hash_password("IntegrationStaffPassword123!"),
                "Integration Receptionist",
            ),
        ).fetchone()[0]
        connection.commit()

    headers = {
        "Authorization": "Bearer "
        + create_access_token(Principal(str(staff_id), "user", "receptionist"))
    }
    assert client.get("/api/admin/dashboard", headers=headers).status_code == 200

    with psycopg.connect(database_url) as connection:
        connection.execute(
            "UPDATE users SET role = 'trainer' WHERE id = %s",
            (staff_id,),
        )
        connection.commit()

    response = client.get("/api/admin/dashboard", headers=headers)

    assert response.status_code == 403


def test_local_payment_confirmation_requires_explicit_test_configuration(
    booking_context: tuple[TestClient, dict[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, created = booking_context
    customer_id, _, _ = _setup(created)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        plan_id = connection.execute(
            "SELECT id FROM pass_plans WHERE name = '10-Session Pass'"
        ).fetchone()[0]
        connection.commit()

    headers = _customer_headers(customer_id)
    purchase = client.post(
        "/api/me/pass-purchases",
        headers=headers,
        json={
            "pass_plan_id": str(plan_id),
            "idempotency_key": f"payment-{uuid.uuid4()}",
        },
    )
    assert purchase.status_code == 201
    intent_id = purchase.json()["id"]

    monkeypatch.delenv("ALLOW_LOCAL_TEST_PAYMENTS", raising=False)
    disabled = client.post(
        f"/api/me/pass-purchases/{intent_id}/confirm",
        headers=headers,
        json={"outcome": "succeed"},
    )
    assert disabled.status_code == 503
    assert disabled.json()["detail"] == "Local payment confirmation is disabled"

    monkeypatch.setenv("ALLOW_LOCAL_TEST_PAYMENTS", "true")
    enabled = client.post(
        f"/api/me/pass-purchases/{intent_id}/confirm",
        headers=headers,
        json={"outcome": "succeed"},
    )

    assert enabled.status_code == 200
    assert enabled.json()["payment_intent"]["status"] == "succeeded"


def test_customer_booking_and_payment_actions_create_audit_records(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    headers = _customer_headers(customer_id)

    booking = client.post(
        "/api/bookings",
        headers=headers,
        json=_booking_request(customer_id, session_id, pass_id),
    )
    _track_successful_booking(booking, created)
    assert booking.status_code == 201

    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        plan_id = connection.execute(
            "SELECT id FROM pass_plans WHERE name = '10-Session Pass'"
        ).fetchone()[0]
        connection.commit()
    purchase = client.post(
        "/api/me/pass-purchases",
        headers=headers,
        json={
            "pass_plan_id": str(plan_id),
            "idempotency_key": f"audit-payment-{uuid.uuid4()}",
        },
    )
    assert purchase.status_code == 201
    intent_id = purchase.json()["id"]
    confirmation = client.post(
        f"/api/me/pass-purchases/{intent_id}/confirm",
        headers=headers,
        json={"outcome": "succeed"},
    )
    assert confirmation.status_code == 200

    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        actions = connection.execute(
            """
            SELECT action
            FROM audit_logs
            WHERE actor_type = 'customer'
              AND entity_id IN (%s, %s)
            ORDER BY id
            """,
            (booking.json()["id"], intent_id),
        ).fetchall()

    assert [row[0] for row in actions] == [
        "booking.create",
        "payment_intent.create",
        "payment_intent.succeed",
    ]


def test_seed_rejects_database_missing_migration_006_prerequisites(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    _, _ = booking_context
    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as connection:
        connection.execute("ALTER TABLE customers DROP COLUMN password_hash")
        connection.execute("DROP TABLE payment_intents")
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="missing: 006"):
            connection.execute(SEED_FILE.read_text(encoding="utf-8"))
        connection.rollback()


def _customer_headers(customer_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + create_access_token(Principal(customer_id, "customer", None))
    }


def _post_as_customer(
    client: TestClient,
    url: str,
    customer_id: str,
    **kwargs,
):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update(_customer_headers(customer_id))
    return client.post(url, headers=headers, **kwargs)


def _new_customer(connection: psycopg.Connection, created: dict[str, list[str]]) -> str:
    while True:
        subscriber = 9_000_000_000 + uuid.uuid4().int % 1_000_000_000
        phone = f"+91{subscriber:010d}"
        if not connection.execute(
            "SELECT EXISTS (SELECT 1 FROM customers WHERE phone_e164 = %s)",
            (phone,),
        ).fetchone()[0]:
            break
    customer_id = connection.execute(
        """
        INSERT INTO customers (full_name, phone_e164, password_hash)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        ("Booking API Customer", phone, hash_password("IntegrationTestPassword123!")),
    ).fetchone()[0]
    created["customers"].append(str(customer_id))
    return str(customer_id)


def _new_session(
    connection: psycopg.Connection,
    created: dict[str, list[str]],
    service_name: str = "Yoga",
) -> str:
    service_id, duration, capacity = connection.execute(
        """
        SELECT id, duration_minutes, default_capacity
        FROM services
        WHERE name = %s
        """,
        (service_name,),
    ).fetchone()
    trainer_id = connection.execute(
        """
        SELECT t.id
        FROM trainers t
        JOIN trainer_services ts ON ts.trainer_id = t.id
        WHERE ts.service_id = %s
          AND t.is_active
        ORDER BY t.id
        LIMIT 1
        """,
        (service_id,),
    ).fetchone()[0]
    session_date = date(2090, 1, 1) + timedelta(days=len(created["sessions"]))
    starts_at = datetime.combine(session_date, time(6, 0), tzinfo=IST)
    session_id = connection.execute(
        """
        INSERT INTO sessions (
            service_id, trainer_id, starts_at, ends_at, capacity
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            service_id,
            trainer_id,
            starts_at,
            starts_at + timedelta(minutes=duration),
            capacity,
        ),
    ).fetchone()[0]
    created["sessions"].append(str(session_id))
    return str(session_id)


def _new_pass(
    connection: psycopg.Connection,
    created: dict[str, list[str]],
    customer_id: str,
    sessions: int = 2,
) -> str:
    plan_id = connection.execute(
        "SELECT id FROM pass_plans WHERE name = '10-Session Pass'"
    ).fetchone()[0]
    pass_id = connection.execute(
        """
        INSERT INTO member_passes (
            customer_id, pass_plan_id, sessions_total, purchased_at, expires_at
        )
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (customer_id, plan_id, sessions, "2089-01-01", "2091-01-01"),
    ).fetchone()[0]
    created["passes"].append(str(pass_id))
    connection.execute(
        """
        INSERT INTO pass_credit_ledger (member_pass_id, delta, reason)
        VALUES (%s, %s, 'purchase')
        """,
        (pass_id, sessions),
    )
    return str(pass_id)


def _setup(
    created: dict[str, list[str]],
    service_name: str = "Yoga",
    sessions: int = 2,
) -> tuple[str, str, str]:
    database_url = os.environ["DATABASE_URL"]
    with psycopg.connect(database_url) as connection:
        customer_id = _new_customer(connection, created)
        session_id = _new_session(connection, created, service_name)
        pass_id = _new_pass(connection, created, customer_id, sessions)
        connection.commit()
    return customer_id, session_id, pass_id


def _booking_request(customer_id: str, session_id: str, pass_id: str, key: str | None = None) -> dict[str, str]:
    return {
        "session_id": session_id,
        "customer_id": customer_id,
        "member_pass_id": pass_id,
        "idempotency_key": key or f"test-{uuid.uuid4()}",
    }


def _booking_count(booking_id: str) -> tuple[int, int]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(
            """
            SELECT
                (SELECT count(*) FROM bookings WHERE id = %s),
                (SELECT count(*) FROM pass_credit_ledger
                 WHERE booking_id = %s AND reason = 'booking_use')
            """,
            (booking_id, booking_id),
        ).fetchone()


def _request_side_effects(idempotency_key: str) -> tuple[int, int]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(
            """
            SELECT count(DISTINCT b.id), count(l.id)
            FROM bookings b
            LEFT JOIN pass_credit_ledger l
              ON l.booking_id = b.id AND l.reason = 'booking_use'
            WHERE b.idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()


def _pass_balance(pass_id: str) -> int:
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(
            "SELECT credits_remaining FROM pass_balances WHERE member_pass_id = %s",
            (pass_id,),
        ).fetchone()[0]


def _track_successful_booking(response, created: dict[str, list[str]]) -> None:
    status_code = response.status_code
    if status_code == 201:
        created["bookings"].append(response.json()["id"])


def _create_pass_booking(
    client: TestClient,
    created: dict[str, list[str]],
    customer_id: str,
    session_id: str,
    pass_id: str,
) -> tuple[dict[str, object], dict[str, str]]:
    request = _booking_request(customer_id, session_id, pass_id)
    response = client.post("/api/bookings", json=request)
    _track_successful_booking(response, created)
    assert response.status_code == 201
    return response.json(), request


def _new_admin_booking(
    connection: psycopg.Connection,
    created: dict[str, list[str]],
    session_id: str,
    customer_id: str,
    payment_mode: str = "online",
) -> str:
    booking_id = connection.execute(
        """
        INSERT INTO bookings (
            booking_ref, session_id, customer_id, price_paise,
            source, status, payment_mode
        )
        VALUES (%s, %s, %s, 0, 'admin', 'confirmed', %s)
        RETURNING id
        """,
        (f"SETUP-{uuid.uuid4()}", session_id, customer_id, payment_mode),
    ).fetchone()[0]
    created["bookings"].append(str(booking_id))
    return str(booking_id)


def _fill_session(
    connection: psycopg.Connection,
    created: dict[str, list[str]],
    session_id: str,
    seats: int,
) -> None:
    for _ in range(seats):
        customer_id = _new_customer(connection, created)
        _new_admin_booking(connection, created, session_id, customer_id)


def _fill_session_to_capacity(
    connection: psycopg.Connection,
    created: dict[str, list[str]],
    session_id: str,
) -> None:
    capacity, occupied = connection.execute(
        """
        SELECT
            s.capacity,
            count(b.id) FILTER (
                WHERE b.status IN ('confirmed', 'pending_payment')
            )
        FROM sessions s
        LEFT JOIN bookings b ON b.session_id = s.id
        WHERE s.id = %s
        GROUP BY s.capacity
        """,
        (session_id,),
    ).fetchone()
    _fill_session(connection, created, session_id, max(capacity - occupied, 0))


def _set_session_in_the_past(
    connection: psycopg.Connection,
    session_id: str,
    day: int = 2,
) -> None:
    connection.execute(
        """
        UPDATE sessions
           SET starts_at = (%s::date + time '06:00') AT TIME ZONE 'Asia/Kolkata',
               ends_at = (%s::date + time '07:00') AT TIME ZONE 'Asia/Kolkata'
         WHERE id = %s
        """,
        (f"2020-01-{day:02d}", f"2020-01-{day:02d}", session_id),
    )


def _ledger_count(booking_id: str, reason: str) -> int:
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(
            """
            SELECT count(*)
            FROM pass_credit_ledger
            WHERE booking_id = %s AND reason = %s
            """,
            (booking_id, reason),
        ).fetchone()[0]


def test_successful_pass_booking(booking_context: tuple[TestClient, dict[str, list[str]]]) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)

    response = client.post("/api/bookings", json=_booking_request(customer_id, session_id, pass_id))

    _track_successful_booking(response, created)
    assert response.status_code == 201
    body = response.json()
    assert body["booking_ref"].startswith("EL-")
    assert body["session_id"] == session_id
    assert body["customer_id"] == customer_id
    assert body["status"] == "confirmed"
    assert body["payment_mode"] == "pass"
    assert body["member_pass_id"] == pass_id
    assert body["price_paise"] == 0
    assert body["hold_expires_at"] is None
    assert _booking_count(body["id"]) == (1, 1)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        assert connection.execute(
            "SELECT count(*) FROM invoices WHERE booking_id = %s", (body["id"],)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM payments p JOIN invoices i ON i.id = p.invoice_id "
            "WHERE i.booking_id = %s",
            (body["id"],),
        ).fetchone()[0] == 0


def test_insufficient_pass_credits_rolls_back(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created, sessions=1)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        other_session = _new_session(connection, created)
        existing_booking = connection.execute(
            """
            INSERT INTO bookings (
                booking_ref, session_id, customer_id, price_paise,
                source, member_pass_id, payment_mode
            )
            VALUES (%s, %s, %s, 0, 'admin', %s, 'pass')
            RETURNING id
            """,
            (f"SETUP-{uuid.uuid4()}", other_session, customer_id, pass_id),
        ).fetchone()[0]
        created["bookings"].append(str(existing_booking))
        connection.execute(
            """
            INSERT INTO pass_credit_ledger (member_pass_id, booking_id, delta, reason)
            VALUES (%s, %s, -1, 'booking_use')
            """,
            (pass_id, existing_booking),
        )
        connection.commit()

    balance_before = _pass_balance(pass_id)
    request = _booking_request(customer_id, session_id, pass_id)
    response = client.post("/api/bookings", json=request)

    assert response.status_code == 422
    assert _request_side_effects(request["idempotency_key"]) == (0, 0)
    assert _pass_balance(pass_id) == balance_before


def test_cancel_booking_refunds_one_credit(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    booking, _ = _create_pass_booking(client, created, customer_id, session_id, pass_id)
    balance_before = _pass_balance(pass_id)

    response = client.post(
        f"/api/bookings/{booking['id']}/cancel",
        json={"customer_id": customer_id},
    )

    assert response.status_code == 200
    assert response.json()["booking"]["status"] == "cancelled"
    assert response.json()["refunded"] is True
    assert _pass_balance(pass_id) == balance_before + 1
    assert _ledger_count(booking["id"], "cancel_refund") == 1


def test_late_cancel_booking_does_not_refund(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        _set_session_in_the_past(connection, session_id)
        connection.commit()

    booking, _ = _create_pass_booking(client, created, customer_id, session_id, pass_id)
    balance_before = _pass_balance(pass_id)
    response = client.post(
        f"/api/bookings/{booking['id']}/cancel",
        json={"customer_id": customer_id},
    )

    assert response.status_code == 200
    assert response.json()["refunded"] is False
    assert _pass_balance(pass_id) == balance_before
    assert _ledger_count(booking["id"], "cancel_refund") == 0


def test_exactly_six_hours_before_session_is_not_too_late(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        result = connection.execute(
            """
            SELECT is_too_late(
                statement_timestamp() + interval '6 hours',
                statement_timestamp(),
                'cancellation_window_hours'
            )
            """
        ).fetchone()[0]

    assert result is False


def test_repeated_cancellation_does_not_refund_twice(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    booking, _ = _create_pass_booking(client, created, customer_id, session_id, pass_id)
    request = {"customer_id": customer_id}

    first = client.post(f"/api/bookings/{booking['id']}/cancel", json=request)
    second = client.post(f"/api/bookings/{booking['id']}/cancel", json=request)

    assert first.status_code == 200
    assert second.status_code == 409
    assert _ledger_count(booking["id"], "cancel_refund") == 1


def test_cancellation_offers_next_waitlist_seat(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    booking, _ = _create_pass_booking(client, created, customer_id, session_id, pass_id)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        _fill_session_to_capacity(connection, created, session_id)
        waitlist_customer = _new_customer(connection, created)
        waitlist_entry = connection.execute(
            """
            INSERT INTO waitlist_entries (session_id, customer_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (session_id, waitlist_customer),
        ).fetchone()[0]
        connection.commit()

    response = client.post(
        f"/api/bookings/{booking['id']}/cancel",
        json={"customer_id": customer_id},
    )

    assert response.status_code == 200
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        assert connection.execute(
            "SELECT status FROM waitlist_entries WHERE id = %s",
            (waitlist_entry,),
        ).fetchone()[0] == "offered"


def test_online_and_studio_cancellation_are_rejected_without_changes(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, _ = _setup(created)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        online_id = _new_admin_booking(connection, created, session_id, customer_id, "online")
        studio_customer = _new_customer(connection, created)
        studio_id = _new_admin_booking(connection, created, session_id, studio_customer, "studio")
        connection.commit()

    online_response = client.post(
        f"/api/bookings/{online_id}/cancel",
        json={"customer_id": customer_id},
    )
    studio_response = client.post(
        f"/api/bookings/{studio_id}/cancel",
        json={"customer_id": studio_customer},
    )

    assert online_response.status_code == 422
    assert studio_response.status_code == 422
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        assert connection.execute(
            "SELECT status FROM bookings WHERE id IN (%s, %s) ORDER BY id",
            (online_id, studio_id),
        ).fetchall() == [("confirmed",), ("confirmed",)]


def test_online_and_studio_reschedule_are_rejected_without_changes(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        online_customer = _new_customer(connection, created)
        online_session = _new_session(connection, created)
        online_target = _new_session(connection, created)
        online_id = _new_admin_booking(
            connection, created, online_session, online_customer, "online"
        )
        studio_customer = _new_customer(connection, created)
        studio_session = _new_session(connection, created)
        studio_target = _new_session(connection, created)
        studio_id = _new_admin_booking(
            connection, created, studio_session, studio_customer, "studio"
        )
        connection.commit()

    online_response = client.post(
        f"/api/bookings/{online_id}/reschedule",
        json={"customer_id": online_customer, "new_session_id": online_target},
    )
    studio_response = client.post(
        f"/api/bookings/{studio_id}/reschedule",
        json={"customer_id": studio_customer, "new_session_id": studio_target},
    )

    assert online_response.status_code == 422
    assert studio_response.status_code == 422


def test_successful_reschedule_replaces_booking_and_preserves_balance(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, original_session_id, pass_id = _setup(created, sessions=2)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        target_session_id = _new_session(connection, created)
        connection.commit()
    original, _ = _create_pass_booking(
        client, created, customer_id, original_session_id, pass_id
    )
    balance_before = _pass_balance(pass_id)

    response = client.post(
        f"/api/bookings/{original['id']}/reschedule",
        json={"customer_id": customer_id, "new_session_id": target_session_id},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["original_booking"]["status"] == "cancelled"
    assert body["replacement_booking"]["session_id"] == target_session_id
    assert body["replacement_booking"]["reschedule_count"] == 1
    assert _pass_balance(pass_id) == balance_before
    assert _ledger_count(original["id"], "cancel_refund") == 1
    assert _ledger_count(body["replacement_booking"]["id"], "booking_use") == 1


def test_incompatible_reschedule_rolls_back_original_booking_and_refund(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, original_session_id, pass_id = _setup(created)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        target_session_id = _new_session(connection, created, "Personal Training")
        connection.commit()
    original, _ = _create_pass_booking(
        client, created, customer_id, original_session_id, pass_id
    )
    balance_before = _pass_balance(pass_id)

    response = client.post(
        f"/api/bookings/{original['id']}/reschedule",
        json={"customer_id": customer_id, "new_session_id": target_session_id},
    )

    assert response.status_code == 422
    assert _pass_balance(pass_id) == balance_before
    assert _ledger_count(original["id"], "cancel_refund") == 0
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        assert connection.execute(
            "SELECT status FROM bookings WHERE id = %s",
            (original["id"],),
        ).fetchone()[0] == "confirmed"


def test_expired_pass_reschedule_rolls_back_original_booking_and_refund(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, original_session_id, pass_id = _setup(created)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        target_session_id = _new_session(connection, created)
        connection.commit()
    original, _ = _create_pass_booking(
        client, created, customer_id, original_session_id, pass_id
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            """
            UPDATE member_passes
               SET expires_at = '2090-01-01 12:00:00+05:30'
             WHERE id = %s
            """,
            (pass_id,),
        )
        connection.commit()
    balance_before = _pass_balance(pass_id)

    response = client.post(
        f"/api/bookings/{original['id']}/reschedule",
        json={"customer_id": customer_id, "new_session_id": target_session_id},
    )

    assert response.status_code == 422
    assert _pass_balance(pass_id) == balance_before
    assert _ledger_count(original["id"], "cancel_refund") == 0
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        assert connection.execute(
            "SELECT status FROM bookings WHERE id = %s",
            (original["id"],),
        ).fetchone()[0] == "confirmed"


def test_full_reschedule_target_is_rejected_without_changes(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, original_session_id, pass_id = _setup(created)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        target_session_id = _new_session(connection, created)
        _fill_session_to_capacity(connection, created, target_session_id)
        connection.commit()
    original, _ = _create_pass_booking(
        client, created, customer_id, original_session_id, pass_id
    )
    balance_before = _pass_balance(pass_id)

    response = client.post(
        f"/api/bookings/{original['id']}/reschedule",
        json={"customer_id": customer_id, "new_session_id": target_session_id},
    )

    assert response.status_code == 409
    assert _pass_balance(pass_id) == balance_before
    assert _ledger_count(original["id"], "cancel_refund") == 0


def test_late_reschedule_is_rejected_without_changes(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, original_session_id, pass_id = _setup(created, sessions=2)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        target_session_id = _new_session(connection, created)
        _set_session_in_the_past(connection, original_session_id, day=3)
        connection.commit()
    original, _ = _create_pass_booking(
        client, created, customer_id, original_session_id, pass_id
    )
    response = client.post(
        f"/api/bookings/{original['id']}/reschedule",
        json={"customer_id": customer_id, "new_session_id": target_session_id},
    )

    assert response.status_code == 409
    assert _ledger_count(original["id"], "cancel_refund") == 0


def test_second_reschedule_is_rejected(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, original_session_id, pass_id = _setup(created, sessions=3)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        first_target_id = _new_session(connection, created)
        second_target_id = _new_session(connection, created)
        connection.commit()
    original, _ = _create_pass_booking(
        client, created, customer_id, original_session_id, pass_id
    )
    first = client.post(
        f"/api/bookings/{original['id']}/reschedule",
        json={"customer_id": customer_id, "new_session_id": first_target_id},
    )
    replacement_id = first.json()["replacement_booking"]["id"]
    second = client.post(
        f"/api/bookings/{replacement_id}/reschedule",
        json={"customer_id": customer_id, "new_session_id": second_target_id},
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_reschedule_offers_original_session_waitlist(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, original_session_id, pass_id = _setup(created)
    original, _ = _create_pass_booking(
        client, created, customer_id, original_session_id, pass_id
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        target_session_id = _new_session(connection, created)
        _fill_session_to_capacity(connection, created, original_session_id)
        waitlist_customer = _new_customer(connection, created)
        waitlist_entry = connection.execute(
            """
            INSERT INTO waitlist_entries (session_id, customer_id)
            VALUES (%s, %s)
            RETURNING id
            """,
            (original_session_id, waitlist_customer),
        ).fetchone()[0]
        connection.commit()

    response = client.post(
        f"/api/bookings/{original['id']}/reschedule",
        json={"customer_id": customer_id, "new_session_id": target_session_id},
    )

    assert response.status_code == 200
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        assert connection.execute(
            "SELECT status FROM waitlist_entries WHERE id = %s",
            (waitlist_entry,),
        ).fetchone()[0] == "offered"


def test_concurrent_reschedule_only_replaces_once(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    _, created = booking_context
    customer_id, original_session_id, pass_id = _setup(created, sessions=3)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        first_target_id = _new_session(connection, created)
        second_target_id = _new_session(connection, created)
        connection.commit()
    with TestClient(app) as setup_client:
        request = _booking_request(customer_id, original_session_id, pass_id)
        original_response = _post_as_customer(
            setup_client,
            "/api/bookings",
            customer_id,
            json=request,
        )
        _track_successful_booking(original_response, created)
        assert original_response.status_code == 201
        original = original_response.json()
    barrier = Barrier(2)

    def submit(target_session_id: str) -> int:
        with TestClient(app) as client:
            barrier.wait()
            return _post_as_customer(
                client,
                f"/api/bookings/{original['id']}/reschedule",
                customer_id,
                json={
                    "customer_id": customer_id,
                    "new_session_id": target_session_id,
                },
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(submit, [first_target_id, second_target_id]))

    assert sorted(statuses) == [200, 409]
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        assert connection.execute(
            """
            SELECT count(*)
            FROM bookings
            WHERE rescheduled_from_booking_id = %s
            """,
            (original["id"],),
        ).fetchone()[0] == 1


def test_pass_belongs_to_different_customer(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    owner_id, session_id, pass_id = _setup(created)
    balance_before = _pass_balance(pass_id)
    other_customer_id = None
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        other_customer_id = _new_customer(connection, created)
        connection.commit()

    request = _booking_request(other_customer_id, session_id, pass_id)
    response = client.post(
        "/api/bookings",
        json=request,
    )

    assert response.status_code == 422
    assert _request_side_effects(request["idempotency_key"]) == (0, 0)
    assert _pass_balance(pass_id) == balance_before
    assert owner_id != other_customer_id


def test_pass_does_not_cover_session_service(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created, "Personal Training")
    balance_before = _pass_balance(pass_id)
    request = _booking_request(customer_id, session_id, pass_id)

    response = client.post("/api/bookings", json=request)

    assert response.status_code == 422
    assert _request_side_effects(request["idempotency_key"]) == (0, 0)
    assert _pass_balance(pass_id) == balance_before


def test_duplicate_idempotency_key_returns_existing_booking(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    request = _booking_request(customer_id, session_id, pass_id)

    first = client.post("/api/bookings", json=request)
    second = client.post("/api/bookings", json=request)

    _track_successful_booking(first, created)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert _booking_count(first.json()["id"]) == (1, 1)


def test_concurrent_duplicate_idempotency_key_returns_one_booking(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    _, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    request = _booking_request(customer_id, session_id, pass_id)
    barrier = Barrier(2)

    def submit() -> tuple[int, str]:
        with TestClient(app) as client:
            barrier.wait()
            response = _post_as_customer(
                client,
                "/api/bookings",
                customer_id,
                json=request,
            )
            return response.status_code, response.json()["id"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))

    for _, booking_id in results:
        if booking_id not in created["bookings"]:
            created["bookings"].append(booking_id)
    assert sorted(status for status, _ in results) == [200, 201]
    assert len({booking_id for _, booking_id in results}) == 1
    booking_id = results[0][1]
    assert _booking_count(booking_id) == (1, 1)


def test_duplicate_idempotency_key_with_different_payload_conflicts(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    other_session = None
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        other_session = _new_session(connection, created)
        connection.commit()
    key = f"test-{uuid.uuid4()}"

    first = client.post(
        "/api/bookings",
        json=_booking_request(customer_id, session_id, pass_id, key),
    )
    second = client.post(
        "/api/bookings",
        json=_booking_request(customer_id, other_session, pass_id, key),
    )

    _track_successful_booking(first, created)
    assert first.status_code == 201
    assert second.status_code == 409
    assert _booking_count(first.json()["id"]) == (1, 1)


def test_existing_active_booking_conflicts(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created, sessions=2)
    first = client.post("/api/bookings", json=_booking_request(customer_id, session_id, pass_id))
    second = client.post("/api/bookings", json=_booking_request(customer_id, session_id, pass_id))

    _track_successful_booking(first, created)
    assert first.status_code == 201
    assert second.status_code == 409
    assert _booking_count(first.json()["id"]) == (1, 1)


def test_exhausted_session_conflicts_without_credit_use(
    booking_context: tuple[TestClient, dict[str, list[str]]],
) -> None:
    client, created = booking_context
    customer_id, session_id, pass_id = _setup(created)
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        for index in range(10):
            other_customer = _new_customer(connection, created)
            booking_id = connection.execute(
                """
                INSERT INTO bookings (
                    booking_ref, session_id, customer_id, price_paise,
                    source, payment_mode
                )
                VALUES (%s, %s, %s, 0, 'admin', 'online')
                RETURNING id
                """,
                (f"SETUP-{uuid.uuid4()}", session_id, other_customer),
            ).fetchone()[0]
            created["bookings"].append(str(booking_id))
        connection.commit()

    balance_before = _pass_balance(pass_id)
    request = _booking_request(
        customer_id,
        session_id,
        pass_id,
        key=f"test-exhausted-{uuid.uuid4()}",
    )
    response = client.post("/api/bookings", json=request)

    assert response.status_code == 409
    assert _request_side_effects(request["idempotency_key"]) == (0, 0)
    assert _pass_balance(pass_id) == balance_before
