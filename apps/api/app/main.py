import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.bookings import router as bookings_router
from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.customer import router as customer_router
from app.api.read import router as read_router

def _cors_origins() -> list[str]:
    configured = os.environ.get("CORS_ALLOWED_ORIGINS")
    if configured:
        origins = [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]
    else:
        origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    if not origins:
        raise RuntimeError("CORS_ALLOWED_ORIGINS must contain at least one origin")
    if "*" in origins:
        raise RuntimeError("Wildcard CORS origins are not allowed")
    return origins


app = FastAPI(title="Elevate Manager API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(auth_router)
app.include_router(customer_router)
app.include_router(admin_router)
app.include_router(read_router)
app.include_router(bookings_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
