from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from psycopg import Connection, errors
from psycopg.types.json import Jsonb

from app.db import get_write_connection
from app.jobs import run_mock_notification_worker, schedule_booking_notifications
from app.security import Principal, hash_password, require_roles


router = APIRouter(
    prefix="/api/admin",
    dependencies=[Depends(require_roles("owner", "receptionist"))],
)


def _audit(
    connection: Connection,
    principal: Principal,
    action: str,
    entity_type: str,
    entity_id: str,
    after: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_user_id, actor_type, action, entity_type, entity_id, after_data
        )
        VALUES (%s, 'user', %s, %s, %s, %s)
        """,
        (UUID(principal.subject_id), action, entity_type, entity_id, Jsonb(jsonable_encoder(after))),
    )


@router.get("/dashboard")
def dashboard(
    connection: Connection = Depends(get_write_connection),
) -> dict[str, int]:
    result = connection.execute(
        """
        SELECT
          (SELECT count(*) FROM customers WHERE anonymized_at IS NULL) AS customers,
          (SELECT count(*) FROM bookings WHERE status = 'confirmed') AS confirmed_bookings,
          (SELECT count(*) FROM bookings WHERE status = 'pending_payment') AS pending_payments,
          (SELECT count(*) FROM member_passes WHERE status = 'active') AS active_passes,
          (SELECT count(*) FROM waitlist_entries WHERE status IN ('waiting','offered')) AS active_waitlist
        """
    ).fetchone()
    return {key: int(value) for key, value in result.items()}


@router.get("/customers")
def list_customers(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT id, full_name, phone_e164, email, whatsapp_opt_in, created_at
            FROM customers
            WHERE anonymized_at IS NULL
            ORDER BY created_at DESC, id
            """
        ).fetchall()
    ]


@router.post("/customers", status_code=201)
def create_customer(
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner", "receptionist")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    try:
        row = connection.execute(
            """
            INSERT INTO customers (full_name, phone_e164, email, whatsapp_opt_in)
            VALUES (%s, %s, %s, COALESCE(%s, false))
            RETURNING id, full_name, phone_e164, email, whatsapp_opt_in
            """,
            (
                str(payload["full_name"]).strip(),
                payload["phone_e164"],
                payload.get("email"),
                payload.get("whatsapp_opt_in"),
            ),
        ).fetchone()
        _audit(connection, principal, "customer.create", "customer", str(row["id"]), dict(row))
        return dict(row)
    except KeyError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=f"Missing field: {error.args[0]}") from None
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Customer phone or email already exists") from None
    except errors.CheckViolation:
        connection.rollback()
        raise HTTPException(status_code=422, detail="Invalid customer data") from None


@router.patch("/customers/{customer_id}")
def update_customer(
    customer_id: UUID,
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner", "receptionist")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    allowed = {
        "full_name": payload.get("full_name"),
        "phone_e164": payload.get("phone_e164"),
        "email": payload.get("email"),
        "whatsapp_opt_in": payload.get("whatsapp_opt_in"),
    }
    updates = {key: value for key, value in allowed.items() if value is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="No editable fields supplied")
    assignments = ", ".join(f"{key} = %s" for key in updates)
    try:
        row = connection.execute(
            f"""
            UPDATE customers
               SET {assignments}
             WHERE id = %s AND anonymized_at IS NULL
            RETURNING id, full_name, phone_e164, email, whatsapp_opt_in
            """,
            (*updates.values(), customer_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Customer not found")
        _audit(connection, principal, "customer.update", "customer", str(customer_id), dict(row))
        return dict(row)
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Customer phone or email already exists") from None


@router.get("/services")
def admin_services(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM services ORDER BY name"
        ).fetchall()
    ]


@router.post("/services", status_code=201)
def create_service(
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    try:
        row = connection.execute(
            """
            INSERT INTO services (
                name, description, duration_minutes, default_capacity,
                price_paise, tax_rate_bps
            )
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, 0))
            RETURNING *
            """,
            (
                payload["name"],
                payload.get("description"),
                payload["duration_minutes"],
                payload["default_capacity"],
                payload["price_paise"],
                payload.get("tax_rate_bps"),
            ),
        ).fetchone()
        _audit(connection, principal, "service.create", "service", str(row["id"]), dict(row))
        return dict(row)
    except KeyError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=f"Missing field: {error.args[0]}") from None
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Service already exists") from None
    except errors.CheckViolation:
        connection.rollback()
        raise HTTPException(status_code=422, detail="Invalid service data") from None


@router.patch("/services/{service_id}")
def update_service(
    service_id: UUID,
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    editable = {
        key: payload[key]
        for key in ("description", "duration_minutes", "default_capacity", "price_paise", "tax_rate_bps", "is_active")
        if key in payload
    }
    if not editable:
        raise HTTPException(status_code=422, detail="No editable fields supplied")
    assignments = ", ".join(f"{key} = %s" for key in editable)
    try:
        row = connection.execute(
            f"UPDATE services SET {assignments} WHERE id = %s RETURNING *",
            (*editable.values(), service_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Service not found")
        _audit(connection, principal, "service.update", "service", str(service_id), dict(row))
        return dict(row)
    except errors.CheckViolation:
        connection.rollback()
        raise HTTPException(status_code=422, detail="Invalid service data") from None


@router.get("/trainers")
def admin_trainers(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM trainers ORDER BY full_name, id"
        ).fetchall()
    ]


@router.post("/trainers", status_code=201)
def create_trainer(
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    try:
        trainer = connection.execute(
            """
            INSERT INTO trainers (full_name, phone_e164)
            VALUES (%s, %s)
            RETURNING *
            """,
            (payload["full_name"], payload.get("phone_e164")),
        ).fetchone()
        for service_id in payload.get("service_ids", []):
            connection.execute(
                "INSERT INTO trainer_services (trainer_id, service_id) VALUES (%s, %s)",
                (trainer["id"], service_id),
            )
        _audit(connection, principal, "trainer.create", "trainer", str(trainer["id"]), dict(trainer))
        return dict(trainer)
    except KeyError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=f"Missing field: {error.args[0]}") from None
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Trainer or service mapping already exists") from None


@router.post("/staff", status_code=201)
def create_staff_user(
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    if payload.get("role") not in {"owner", "receptionist", "trainer"}:
        raise HTTPException(status_code=422, detail="Invalid staff role")
    try:
        user = connection.execute(
            """
            INSERT INTO users (email, password_hash, full_name, role)
            VALUES (%s, %s, %s, %s)
            RETURNING id, email, full_name, role, is_active
            """,
            (
                payload["email"],
                hash_password(payload["password"]),
                payload["full_name"],
                payload["role"],
            ),
        ).fetchone()
        _audit(connection, principal, "staff.create", "user", str(user["id"]), dict(user))
        return dict(user)
    except ValueError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from None
    except KeyError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=f"Missing field: {error.args[0]}") from None
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Staff email already exists") from None


@router.get("/passes")
def admin_passes(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT mp.*, c.full_name AS customer_name, pp.name AS plan_name,
                   pb.credits_remaining
            FROM member_passes mp
            JOIN customers c ON c.id = mp.customer_id
            JOIN pass_plans pp ON pp.id = mp.pass_plan_id
            JOIN pass_balances pb ON pb.member_pass_id = mp.id
            ORDER BY mp.created_at DESC, mp.id
            """
        ).fetchall()
    ]


@router.get("/pass-plans")
def admin_pass_plans(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM pass_plans ORDER BY name"
        ).fetchall()
    ]


@router.post("/pass-plans", status_code=201)
def create_pass_plan(
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    try:
        plan = connection.execute(
            """
            INSERT INTO pass_plans (
                name, session_count, validity_days, price_paise, tax_rate_bps
            )
            VALUES (%s, %s, %s, %s, COALESCE(%s, 0))
            RETURNING *
            """,
            (
                payload["name"],
                payload["session_count"],
                payload["validity_days"],
                payload["price_paise"],
                payload.get("tax_rate_bps"),
            ),
        ).fetchone()
        _audit(connection, principal, "pass_plan.create", "pass_plan", str(plan["id"]), dict(plan))
        return dict(plan)
    except KeyError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=f"Missing field: {error.args[0]}") from None
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Pass plan already exists") from None
    except errors.CheckViolation:
        connection.rollback()
        raise HTTPException(status_code=422, detail="Invalid pass plan") from None


@router.post("/sessions", status_code=201)
def create_session(
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner", "receptionist")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    try:
        row = connection.execute(
            """
            INSERT INTO sessions (
                service_id, trainer_id, starts_at, ends_at, capacity
            )
            VALUES (%s, %s, %s, %s, COALESCE(%s, (
                SELECT default_capacity FROM services WHERE id = %s
            )))
            RETURNING *
            """,
            (
                payload["service_id"],
                payload["trainer_id"],
                payload["starts_at"],
                payload["ends_at"],
                payload.get("capacity"),
                payload["service_id"],
            ),
        ).fetchone()
        _audit(connection, principal, "session.create", "session", str(row["id"]), dict(row))
        return dict(row)
    except KeyError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=f"Missing field: {error.args[0]}") from None
    except errors.CheckViolation as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=error.diag.message_primary or "Invalid session") from None
    except errors.DatabaseError:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Session violates an existing constraint") from None


@router.get("/bookings")
def admin_bookings(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT b.*, c.full_name AS customer_name, s.starts_at,
                   svc.name AS service_name
            FROM bookings b
            JOIN customers c ON c.id = b.customer_id
            JOIN sessions s ON s.id = b.session_id
            JOIN services svc ON svc.id = s.service_id
            ORDER BY s.starts_at DESC, b.id
            """
        ).fetchall()
    ]


@router.post("/attendance", status_code=201)
def mark_attendance(
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner", "receptionist")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    if payload.get("status") not in {"present", "absent", "excused"}:
        raise HTTPException(status_code=422, detail="Invalid attendance status")
    row = connection.execute(
        """
        INSERT INTO attendance_records (booking_id, status, marked_by, notes)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (booking_id) DO UPDATE
          SET status = EXCLUDED.status, marked_by = EXCLUDED.marked_by,
              marked_at = statement_timestamp(), notes = EXCLUDED.notes
        RETURNING *
        """,
        (payload["booking_id"], payload["status"], UUID(principal.subject_id), payload.get("notes")),
    ).fetchone()
    _audit(connection, principal, "attendance.mark", "booking", str(payload["booking_id"]), dict(row))
    return dict(row)


@router.get("/waitlist")
def admin_waitlist(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT w.*, c.full_name AS customer_name
            FROM waitlist_entries w
            JOIN customers c ON c.id = w.customer_id
            ORDER BY w.created_at, w.id
            """
        ).fetchall()
    ]


@router.get("/payments")
def admin_payments(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT p.*, i.invoice_number, c.full_name AS customer_name
            FROM payments p
            JOIN invoices i ON i.id = p.invoice_id
            JOIN customers c ON c.id = i.customer_id
            ORDER BY p.received_at DESC, p.id
            """
        ).fetchall()
    ]


@router.get("/invoices")
def admin_invoices(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT ib.*, c.full_name AS customer_name
            FROM invoice_balances ib
            JOIN invoices i ON i.id = ib.invoice_id
            JOIN customers c ON c.id = i.customer_id
            ORDER BY i.issue_date DESC, i.id
            """
        ).fetchall()
    ]


@router.get("/settings")
def admin_settings(
    connection: Connection = Depends(get_write_connection),
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute("SELECT * FROM business_settings ORDER BY key").fetchall()]


@router.post("/automation/bookings/{booking_id}/schedule")
def schedule_booking_jobs(
    booking_id: UUID,
    connection: Connection = Depends(get_write_connection),
) -> dict[str, int]:
    return {"created": schedule_booking_notifications(connection, booking_id)}


@router.post("/automation/notifications/run")
def run_notification_jobs(
    connection: Connection = Depends(get_write_connection),
) -> dict[str, int]:
    return {"processed": run_mock_notification_worker(connection)}


@router.patch("/settings/{key}")
def update_setting(
    key: str,
    payload: dict[str, Any] = Body(...),
    principal: Principal = Depends(require_roles("owner")),
    connection: Connection = Depends(get_write_connection),
) -> dict[str, Any]:
    if "value" not in payload:
        raise HTTPException(status_code=422, detail="value is required")
    row = connection.execute(
        """
        UPDATE business_settings
           SET value = %s, updated_by = %s
         WHERE key = %s
        RETURNING *
        """,
        (Jsonb(payload["value"]), UUID(principal.subject_id), key),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Setting not found")
    _audit(connection, principal, "setting.update", "business_setting", key, dict(row))
    return dict(row)
