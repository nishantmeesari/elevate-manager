import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.db import get_connection

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    subject_id: str
    principal_type: str
    role: str | None


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
    )
    return "scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_value, digest_value = encoded.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_value.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _secret() -> bytes:
    secret = os.environ.get("AUTH_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    return secret.encode("utf-8")


def create_access_token(principal: Principal, expires_seconds: int = 3600) -> str:
    issued_at = int(time.time())
    payload = {
        "sub": principal.subject_id,
        "typ": principal.principal_type,
        "role": principal.role,
        "iat": issued_at,
        "exp": issued_at + expires_seconds,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(_secret(), encoded, hashlib.sha256).digest()
    return encoded.decode("ascii") + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def decode_access_token(token: str) -> Principal:
    try:
        encoded, signature_value = token.split(".", 1)
        expected = hmac.new(
            _secret(),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
        supplied = base64.urlsafe_b64decode(signature_value + "=" * (-len(signature_value) % 4))
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        if int(payload["exp"]) <= int(time.time()):
            raise ValueError
        return Principal(
            subject_id=str(payload["sub"]),
            principal_type=str(payload["typ"]),
            role=payload.get("role"),
        )
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid or expired access token") from None


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required")
    principal = decode_access_token(credentials.credentials)
    if principal.principal_type not in {"customer", "user"}:
        raise HTTPException(status_code=401, detail="Invalid access token")
    connections = get_connection()
    connection = next(connections)
    try:
        if principal.principal_type == "customer":
            row = connection.execute(
                """
                SELECT id
                FROM customers
                WHERE id = %s AND anonymized_at IS NULL AND password_hash IS NOT NULL
                """,
                (principal.subject_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=401, detail="Account is inactive or unavailable")
        else:
            row = connection.execute(
                "SELECT role FROM users WHERE id = %s AND is_active",
                (principal.subject_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=401, detail="Account is inactive or unavailable")
            principal = Principal(principal.subject_id, "user", row["role"])
        return principal
    finally:
        try:
            next(connections)
        except StopIteration:
            pass


def require_roles(*roles: str):
    def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if principal.principal_type != "user" or principal.role not in roles:
            raise HTTPException(status_code=403, detail="Administrator permission required")
        return principal

    return dependency


def require_customer(principal: Principal = Depends(get_current_principal)) -> Principal:
    if principal.principal_type != "customer":
        raise HTTPException(status_code=403, detail="Customer account required")
    return principal
