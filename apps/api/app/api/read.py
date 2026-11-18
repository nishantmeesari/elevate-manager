from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg import Connection

from app.db import get_connection
from app.schemas.read import (
    ReadinessResponse,
    ServiceResponse,
    SessionResponse,
    TrainerResponse,
)

router = APIRouter(prefix="/api")

SESSION_SELECT = """
    SELECT
        s.id,
        s.service_id,
        svc.name AS service_name,
        s.trainer_id,
        t.full_name AS trainer_name,
        s.starts_at,
        s.ends_at,
        s.capacity,
        (
            SELECT COUNT(*)::integer
            FROM bookings b
            WHERE b.session_id = s.id
              AND b.status IN ('confirmed', 'pending_payment')
        ) + (
            SELECT COUNT(*)::integer
            FROM waitlist_entries w
            WHERE w.session_id = s.id
              AND w.status = 'offered'
              AND w.offer_expires_at > now()
        ) AS occupied_seats
    FROM sessions s
    JOIN services svc ON svc.id = s.service_id
    JOIN trainers t ON t.id = s.trainer_id
"""


def to_session_response(row: dict) -> SessionResponse:
    occupied = row["occupied_seats"]
    return SessionResponse(
        **row,
        available_seats=max(row["capacity"] - occupied, 0),
    )


@router.get("/readiness", response_model=ReadinessResponse)
def readiness(connection: Connection = Depends(get_connection)) -> ReadinessResponse:
    connection.execute("SELECT 1")
    return ReadinessResponse(status="ok", database="connected")


@router.get("/services", response_model=list[ServiceResponse])
def list_active_services(
    connection: Connection = Depends(get_connection),
) -> list[ServiceResponse]:
    rows = connection.execute(
        """
        SELECT id, name, description, duration_minutes, default_capacity, price_paise
        FROM services
        WHERE is_active
        ORDER BY name
        """
    ).fetchall()
    return [ServiceResponse(**row) for row in rows]


@router.get("/trainers", response_model=list[TrainerResponse])
def list_active_trainers(
    connection: Connection = Depends(get_connection),
) -> list[TrainerResponse]:
    rows = connection.execute(
        """
        SELECT
            t.id,
            t.full_name,
            COALESCE(
                array_agg(s.name ORDER BY s.name) FILTER (WHERE s.id IS NOT NULL),
                ARRAY[]::text[]
            ) AS services
        FROM trainers t
        LEFT JOIN trainer_services ts ON ts.trainer_id = t.id
        LEFT JOIN services s ON s.id = ts.service_id AND s.is_active
        WHERE t.is_active
        GROUP BY t.id, t.full_name
        ORDER BY t.full_name
        """
    ).fetchall()
    return [TrainerResponse(**row) for row in rows]


@router.get("/sessions", response_model=list[SessionResponse])
def list_upcoming_sessions(
    limit: int = Query(default=50, ge=1, le=100),
    connection: Connection = Depends(get_connection),
) -> list[SessionResponse]:
    rows = connection.execute(
        SESSION_SELECT
        + """
        WHERE s.status = 'scheduled'
          AND s.starts_at >= now()
        ORDER BY s.starts_at, s.id
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [to_session_response(row) for row in rows]


@router.get("/sessions/{session_id}/availability", response_model=SessionResponse)
def get_session_availability(
    session_id: UUID,
    connection: Connection = Depends(get_connection),
) -> SessionResponse:
    row = connection.execute(
        SESSION_SELECT
        + """
        WHERE s.id = %s
          AND s.status = 'scheduled'
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Scheduled session not found")
    return to_session_response(row)
