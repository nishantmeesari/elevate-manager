from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PassBookingCreate(BaseModel):
    session_id: UUID
    customer_id: UUID
    member_pass_id: UUID
    idempotency_key: str = Field(min_length=1)

    @field_validator("idempotency_key")
    @classmethod
    def reject_blank_idempotency_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("idempotency_key must not be blank")
        return value


class BookingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    booking_ref: str
    session_id: UUID
    customer_id: UUID
    status: str
    payment_mode: str
    member_pass_id: UUID
    price_paise: int
    hold_expires_at: datetime | None
    reschedule_count: int


class BookingCancellationRequest(BaseModel):
    customer_id: UUID


class BookingRescheduleRequest(BaseModel):
    customer_id: UUID
    new_session_id: UUID


class CancellationResponse(BaseModel):
    booking: BookingResponse
    refunded: bool


class RescheduleResponse(BaseModel):
    original_booking: BookingResponse
    replacement_booking: BookingResponse
