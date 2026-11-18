import hmac
import os

from fastapi import APIRouter, Depends, HTTPException
from psycopg import Connection, errors

from app.db import get_write_connection
from app.schemas.auth import (
    AdminBootstrapRequest,
    CustomerLoginRequest,
    CustomerRegisterRequest,
    StaffLoginRequest,
    TokenResponse,
)
from app.security import Principal, create_access_token, hash_password, verify_password


router = APIRouter(prefix="/api/auth")


@router.post("/register", response_model=TokenResponse, status_code=201)
def register_customer(
    request: CustomerRegisterRequest,
    connection: Connection = Depends(get_write_connection),
) -> TokenResponse:
    try:
        password_hash = hash_password(request.password)
        customer = connection.execute(
            """
            INSERT INTO customers (full_name, phone_e164, email, password_hash)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (
                request.full_name.strip(),
                request.phone_e164,
                request.email.strip(),
                password_hash,
            ),
        ).fetchone()
    except ValueError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from None
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Email or phone is already registered") from None
    except errors.DatabaseError:
        connection.rollback()
        raise HTTPException(status_code=503, detail="Database operation failed") from None
    return TokenResponse(
        access_token=create_access_token(
            Principal(str(customer["id"]), "customer", None)
        )
    )


@router.post("/login", response_model=TokenResponse)
def login_customer(
    request: CustomerLoginRequest,
    connection: Connection = Depends(get_write_connection),
) -> TokenResponse:
    customer = connection.execute(
        """
        SELECT id, password_hash
        FROM customers
        WHERE email = %s AND anonymized_at IS NULL
        """,
        (request.email.strip(),),
    ).fetchone()
    if customer is None or customer["password_hash"] is None or not verify_password(
        request.password, customer["password_hash"]
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return TokenResponse(
        access_token=create_access_token(
            Principal(str(customer["id"]), "customer", None)
        )
    )


@router.post("/staff/login", response_model=TokenResponse)
def login_staff(
    request: StaffLoginRequest,
    connection: Connection = Depends(get_write_connection),
) -> TokenResponse:
    user = connection.execute(
        """
        SELECT id, role, password_hash
        FROM users
        WHERE email = %s AND is_active
        """,
        (request.email.strip(),),
    ).fetchone()
    if user is None or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    connection.execute(
        "UPDATE users SET last_login_at = statement_timestamp() WHERE id = %s",
        (user["id"],),
    )
    return TokenResponse(
        access_token=create_access_token(
            Principal(str(user["id"]), "user", user["role"])
        )
    )


@router.post("/bootstrap-admin", response_model=TokenResponse, status_code=201)
def bootstrap_admin(
    request: AdminBootstrapRequest,
    connection: Connection = Depends(get_write_connection),
) -> TokenResponse:
    configured = os.environ.get("INITIAL_ADMIN_BOOTSTRAP_TOKEN")
    if not configured or not hmac.compare_digest(request.bootstrap_token, configured):
        raise HTTPException(status_code=403, detail="Invalid bootstrap token")
    connection.execute("SELECT pg_advisory_xact_lock(hashtext('elevate-admin-bootstrap'))")
    if connection.execute(
        "SELECT EXISTS (SELECT 1 FROM users WHERE role = 'owner')"
    ).fetchone()["exists"]:
        raise HTTPException(status_code=409, detail="An administrator already exists")
    try:
        user = connection.execute(
            """
            INSERT INTO users (email, password_hash, full_name, role)
            VALUES (%s, %s, %s, 'owner')
            RETURNING id, role
            """,
            (
                request.email.strip(),
                hash_password(request.password),
                request.full_name.strip(),
            ),
        ).fetchone()
    except ValueError as error:
        connection.rollback()
        raise HTTPException(status_code=422, detail=str(error)) from None
    except errors.UniqueViolation:
        connection.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered") from None
    return TokenResponse(
        access_token=create_access_token(
            Principal(str(user["id"]), "user", user["role"])
        )
    )
