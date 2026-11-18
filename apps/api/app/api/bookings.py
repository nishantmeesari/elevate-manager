import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from psycopg import Connection, errors
from psycopg.types.json import Jsonb

from app.db import get_write_connection
from app.jobs import schedule_booking_notifications, schedule_event_notification
from app.schemas.bookings import (
    BookingCancellationRequest,
    BookingResponse,
    BookingRescheduleRequest,
    CancellationResponse,
    PassBookingCreate,
    RescheduleResponse,
)
from app.security import Principal, require_customer

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)

BOOKING_FIELDS = (
    "session_id",
    "customer_id",
    "member_pass_id",
)
BOOKING_SELECT = """
    SELECT id, booking_ref, session_id, customer_id, status, payment_mode,
           member_pass_id, price_paise, hold_expires_at, reschedule_count
    FROM bookings
"""


def _audit_customer(
    connection: Connection,
    customer_id: UUID,
    action: str,
    entity_type: str,
    entity_id: UUID,
    after: Any,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_logs (
            actor_type, action, entity_type, entity_id, after_data
        )
        VALUES ('customer', %s, %s, %s, %s)
        """,
        (action, entity_type, str(entity_id), Jsonb(jsonable_encoder(after))),
    )


def _same_idempotent_request(row: dict[str, Any], request: PassBookingCreate) -> bool:
    return (
        all(row[field] == getattr(request, field) for field in BOOKING_FIELDS)
        and row["status"] == "confirmed"
        and row["payment_mode"] == "pass"
        and row["price_paise"] == 0
    )


def _find_existing_booking(
    connection: Connection,
    request: PassBookingCreate,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, booking_ref, session_id, customer_id, status, payment_mode,
               member_pass_id, price_paise, hold_expires_at, reschedule_count
        FROM bookings
        WHERE idempotency_key = %s
        """,
        (request.idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if not _same_idempotent_request(row, request):
        raise HTTPException(
            status_code=409,
            detail="Idempotency key was already used for a different booking",
        )
    return row


def _resource_exists(connection: Connection, table: str, resource_id: UUID) -> bool:
    allowed_tables: dict[str, str] = {
        "customers": "customers",
        "sessions": "sessions",
        "member_passes": "member_passes",
    }
    if table not in allowed_tables:
        raise ValueError("Unexpected resource table")
    return (
        connection.execute(
            f"SELECT EXISTS (SELECT 1 FROM {allowed_tables[table]} WHERE id = %s)",
            (resource_id,),
        ).fetchone()["exists"]
    )


def _handle_foreign_key_failure(
    connection: Connection,
    request: PassBookingCreate,
) -> HTTPException:
    if not _resource_exists(connection, "customers", request.customer_id):
        return HTTPException(status_code=404, detail="Customer not found")
    if not _resource_exists(connection, "sessions", request.session_id):
        return HTTPException(status_code=404, detail="Session not found")
    if not _resource_exists(connection, "member_passes", request.member_pass_id):
        return HTTPException(status_code=404, detail="Pass not found")
    return HTTPException(
        status_code=422,
        detail="Pass does not belong to the customer",
    )


def _handle_check_failure(error: errors.CheckViolation) -> HTTPException:
    message = error.diag.message_primary or ""
    if "full" in message or "not open for booking" in message:
        return HTTPException(status_code=409, detail="Session is not available")
    if any(
        phrase in message
        for phrase in (
            "insufficient pass credits",
            "pass is not active",
            "pass expires before this session",
            "pass does not cover this service",
        )
    ):
        return HTTPException(status_code=422, detail="Pass cannot be used for this booking")
    return HTTPException(status_code=422, detail="Booking could not be created")


def _handle_unique_failure(
    connection: Connection,
    request: PassBookingCreate,
    error: errors.UniqueViolation,
) -> dict[str, Any] | None:
    existing = _find_existing_booking(connection, request)
    if existing is not None:
        return existing
    constraint = error.diag.constraint_name or ""
    if constraint == "uq_booking_customer_session":
        raise HTTPException(
            status_code=409,
            detail="Customer already has an active booking for this session",
        )
    if constraint == "uq_pass_use_per_booking":
        raise HTTPException(status_code=409, detail="Booking credit was already used")
    if "idempotency_key" in constraint:
        raise HTTPException(status_code=409, detail="Idempotency key is already in use")
    logger.error("Unexpected booking uniqueness violation: %s", constraint)
    raise HTTPException(status_code=503, detail="Database operation failed")


def _mutation_check_failure(error: errors.CheckViolation) -> HTTPException:
    message = (error.diag.message_primary or "").lower()
    if "business setting" in message:
        return HTTPException(status_code=503, detail="Booking policy is unavailable")
    if (
        "full" in message
        or "not open for booking" in message
        or "reschedule limit reached" in message
    ):
        return HTTPException(status_code=409, detail="Booking operation is not available")
    if any(
        phrase in message
        for phrase in (
            "insufficient pass credits",
            "pass is not active",
            "pass expires before this session",
            "pass does not cover this service",
        )
    ):
        return HTTPException(status_code=422, detail="Pass cannot be used for this booking")
    return HTTPException(status_code=409, detail="Booking operation could not be completed")


def _load_booking_for_update(
    connection: Connection,
    booking_id: UUID,
) -> dict[str, Any] | None:
    return connection.execute(
        BOOKING_SELECT + " WHERE id = %s FOR UPDATE",
        (booking_id,),
    ).fetchone()


def _load_sessions_for_update(
    connection: Connection,
    original_session_id: UUID,
    target_session_id: UUID,
) -> dict[UUID, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, starts_at, status
        FROM sessions
        WHERE id IN (%s, %s)
        ORDER BY id
        FOR UPDATE
        """,
        (original_session_id, target_session_id),
    ).fetchall()
    return {row["id"]: row for row in rows}


def _booking_operation_error(
    connection: Connection,
    status_code: int,
    detail: str,
) -> None:
    connection.rollback()
    raise HTTPException(status_code=status_code, detail=detail)


def _booking_status_error(connection: Connection, operation: str) -> None:
    _booking_operation_error(
        connection,
        409,
        f"Booking cannot be {operation} in its current state",
    )


@router.post(
    "/bookings",
    response_model=BookingResponse,
    status_code=201,
)
def create_pass_booking(
    request: PassBookingCreate,
    response: Response,
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> BookingResponse:
    if principal.subject_id != str(request.customer_id):
        raise HTTPException(status_code=403, detail="Customer identity does not match the booking")
    existing = _find_existing_booking(connection, request)
    if existing is not None:
        response.status_code = 200
        return BookingResponse(**existing)

    booking_ref = f"EL-{uuid4()}"
    try:
        booking = connection.execute(
            """
            INSERT INTO bookings (
                booking_ref,
                session_id,
                customer_id,
                status,
                price_paise,
                source,
                idempotency_key,
                member_pass_id,
                payment_mode
            )
            VALUES (%s, %s, %s, 'confirmed', 0, 'website', %s, %s, 'pass')
            RETURNING id, booking_ref, session_id, customer_id, status, payment_mode,
                      member_pass_id, price_paise, hold_expires_at, reschedule_count
            """,
            (
                booking_ref,
                request.session_id,
                request.customer_id,
                request.idempotency_key,
                request.member_pass_id,
            ),
        ).fetchone()

        connection.execute(
            """
            INSERT INTO pass_credit_ledger (
                member_pass_id,
                booking_id,
                delta,
                reason
            )
            VALUES (%s, %s, -1, 'booking_use')
            """,
            (request.member_pass_id, booking["id"]),
        )
        schedule_booking_notifications(connection, booking["id"])
        _audit_customer(
            connection,
            request.customer_id,
            "booking.create",
            "booking",
            booking["id"],
            booking,
        )
    except errors.UniqueViolation as error:
        connection.rollback()
        replay = _handle_unique_failure(connection, request, error)
        if replay is not None:
            response.status_code = 200
            return BookingResponse(**replay)
        raise AssertionError("unreachable")
    except errors.ForeignKeyViolation:
        connection.rollback()
        raise _handle_foreign_key_failure(connection, request) from None
    except errors.CheckViolation as error:
        connection.rollback()
        raise _handle_check_failure(error) from None
    except errors.DatabaseError:
        connection.rollback()
        logger.exception("Unexpected booking database failure")
        raise HTTPException(status_code=503, detail="Database operation failed") from None

    return BookingResponse(**booking)


@router.post(
    "/bookings/{booking_id}/cancel",
    response_model=CancellationResponse,
)
def cancel_booking(
    booking_id: UUID,
    request: BookingCancellationRequest,
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> CancellationResponse:
    if principal.subject_id != str(request.customer_id):
        raise HTTPException(status_code=403, detail="Customer identity does not match the booking")
    try:
        booking = _load_booking_for_update(connection, booking_id)
        if booking is None:
            _booking_operation_error(connection, 404, "Booking not found")
        if booking["customer_id"] != request.customer_id:
            _booking_operation_error(connection, 409, "Booking does not belong to the customer")
        if booking["payment_mode"] != "pass":
            _booking_operation_error(
                connection,
                422,
                "Only pass-based bookings can be cancelled",
            )
        if booking["status"] != "confirmed":
            _booking_status_error(connection, "cancelled")

        session = connection.execute(
            """
            SELECT id, starts_at, status
            FROM sessions
            WHERE id = %s
            FOR UPDATE
            """,
            (booking["session_id"],),
        ).fetchone()
        if session is None:
            _booking_operation_error(connection, 404, "Session not found")

        too_late = connection.execute(
            """
            SELECT is_too_late(%s, statement_timestamp(), 'cancellation_window_hours')
                AS too_late
            """,
            (session["starts_at"],),
        ).fetchone()["too_late"]

        connection.execute(
            """
            UPDATE bookings
               SET status = 'cancelled',
                   cancelled_at = statement_timestamp(),
                   cancellation_reason = %s,
                   is_late_cancellation = %s
             WHERE id = %s
            """,
            (
                "late cancellation" if too_late else "customer cancellation",
                too_late,
                booking_id,
            ),
        )

        if not too_late:
            connection.execute(
                """
                INSERT INTO pass_credit_ledger (
                    member_pass_id, booking_id, delta, reason
                )
                VALUES (%s, %s, 1, 'cancel_refund')
                """,
                (booking["member_pass_id"], booking_id),
            )

        connection.execute(
            "SELECT offer_next_waitlist_seat(%s, statement_timestamp())",
            (booking["session_id"],),
        )
        schedule_event_notification(
            connection,
            booking["customer_id"],
            booking_id,
            "cancellation",
        )
        cancelled = connection.execute(
            BOOKING_SELECT + " WHERE id = %s",
            (booking_id,),
        ).fetchone()
        _audit_customer(
            connection,
            booking["customer_id"],
            "booking.cancel",
            "booking",
            booking_id,
            cancelled,
        )
        return CancellationResponse(
            booking=BookingResponse(**cancelled),
            refunded=not too_late,
        )
    except HTTPException:
        connection.rollback()
        raise
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(
            status_code=409,
            detail="Booking has already been cancelled or refunded",
        ) from None
    except errors.CheckViolation as error:
        connection.rollback()
        raise _mutation_check_failure(error) from None
    except errors.ForeignKeyViolation:
        connection.rollback()
        raise HTTPException(status_code=404, detail="Booking or session not found") from None
    except errors.DatabaseError:
        connection.rollback()
        logger.exception("Unexpected booking cancellation database failure")
        raise HTTPException(status_code=503, detail="Database operation failed") from None


@router.post(
    "/bookings/{booking_id}/reschedule",
    response_model=RescheduleResponse,
)
def reschedule_booking(
    booking_id: UUID,
    request: BookingRescheduleRequest,
    principal: Principal = Depends(require_customer),
    connection: Connection = Depends(get_write_connection),
) -> RescheduleResponse:
    if principal.subject_id != str(request.customer_id):
        raise HTTPException(status_code=403, detail="Customer identity does not match the booking")
    try:
        booking = _load_booking_for_update(connection, booking_id)
        if booking is None:
            _booking_operation_error(connection, 404, "Booking not found")
        if booking["customer_id"] != request.customer_id:
            _booking_operation_error(connection, 409, "Booking does not belong to the customer")
        if booking["payment_mode"] != "pass":
            _booking_operation_error(
                connection,
                422,
                "Only pass-based bookings can be rescheduled",
            )
        if booking["status"] != "confirmed":
            _booking_status_error(connection, "rescheduled")
        if booking["session_id"] == request.new_session_id:
            _booking_operation_error(
                connection,
                409,
                "Replacement session must be different from the original session",
            )

        sessions = _load_sessions_for_update(
            connection,
            booking["session_id"],
            request.new_session_id,
        )
        original_session = sessions.get(booking["session_id"])
        target_session = sessions.get(request.new_session_id)
        if original_session is None:
            _booking_operation_error(connection, 404, "Original session not found")
        if target_session is None:
            _booking_operation_error(connection, 404, "Replacement session not found")
        if target_session["status"] != "scheduled":
            _booking_operation_error(connection, 409, "Replacement session is not available")
        now = connection.execute(
            "SELECT statement_timestamp() AS current_time"
        ).fetchone()["current_time"]
        if target_session["starts_at"] <= now:
            _booking_operation_error(connection, 409, "Replacement session is not available")

        too_late = connection.execute(
            """
            SELECT is_too_late(
                %s,
                statement_timestamp(),
                'reschedule_window_hours'
            ) AS too_late
            """,
            (original_session["starts_at"],),
        ).fetchone()["too_late"]
        if too_late:
            _booking_operation_error(
                connection,
                409,
                "Booking cannot be rescheduled within the six-hour window",
            )

        connection.execute(
            """
            UPDATE bookings
               SET status = 'cancelled',
                   cancelled_at = statement_timestamp(),
                   cancellation_reason = 'rescheduled',
                   is_late_cancellation = false
             WHERE id = %s
            """,
            (booking_id,),
        )
        connection.execute(
            """
            INSERT INTO pass_credit_ledger (
                member_pass_id, booking_id, delta, reason
            )
            VALUES (%s, %s, 1, 'cancel_refund')
            """,
            (booking["member_pass_id"], booking_id),
        )
        replacement = connection.execute(
            """
            INSERT INTO bookings (
                booking_ref,
                session_id,
                customer_id,
                status,
                price_paise,
                source,
                rescheduled_from_booking_id,
                member_pass_id,
                payment_mode
            )
            VALUES (%s, %s, %s, 'confirmed', 0, 'website', %s, %s, 'pass')
            RETURNING id, booking_ref, session_id, customer_id, status, payment_mode,
                      member_pass_id, price_paise, hold_expires_at, reschedule_count
            """,
            (
                f"EL-{uuid4()}",
                request.new_session_id,
                request.customer_id,
                booking_id,
                booking["member_pass_id"],
            ),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO pass_credit_ledger (
                member_pass_id, booking_id, delta, reason
            )
            VALUES (%s, %s, -1, 'booking_use')
            """,
            (booking["member_pass_id"], replacement["id"]),
        )
        connection.execute(
            "SELECT offer_next_waitlist_seat(%s, statement_timestamp())",
            (booking["session_id"],),
        )
        schedule_event_notification(
            connection,
            booking["customer_id"],
            booking_id,
            "reschedule",
        )
        schedule_booking_notifications(connection, replacement["id"])
        original = connection.execute(
            BOOKING_SELECT + " WHERE id = %s",
            (booking_id,),
        ).fetchone()
        _audit_customer(
            connection,
            booking["customer_id"],
            "booking.reschedule",
            "booking",
            booking_id,
            {
                "original_booking": original,
                "replacement_booking": replacement,
            },
        )
        return RescheduleResponse(
            original_booking=BookingResponse(**original),
            replacement_booking=BookingResponse(**replacement),
        )
    except HTTPException:
        connection.rollback()
        raise
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(
            status_code=409,
            detail="Booking cannot be rescheduled because a conflicting booking exists",
        ) from None
    except errors.CheckViolation as error:
        connection.rollback()
        raise _mutation_check_failure(error) from None
    except errors.ForeignKeyViolation:
        connection.rollback()
        raise HTTPException(status_code=404, detail="Booking or session not found") from None
    except errors.DatabaseError:
        connection.rollback()
        logger.exception("Unexpected booking reschedule database failure")
        raise HTTPException(status_code=503, detail="Database operation failed") from None
