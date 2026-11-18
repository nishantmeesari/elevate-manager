from fastapi import FastAPI

from app.api.bookings import router as bookings_router
from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.customer import router as customer_router
from app.api.read import router as read_router

app = FastAPI(title="Elevate Manager API", version="0.1.0")
app.include_router(auth_router)
app.include_router(customer_router)
app.include_router(admin_router)
app.include_router(read_router)
app.include_router(bookings_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
