from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ServiceResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    duration_minutes: int = Field(gt=0)
    default_capacity: int = Field(gt=0)
    price_paise: int = Field(ge=0)


class TrainerResponse(BaseModel):
    id: UUID
    full_name: str
    services: list[str]


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service_id: UUID
    service_name: str
    trainer_id: UUID
    trainer_name: str
    starts_at: datetime
    ends_at: datetime
    capacity: int = Field(gt=0)
    occupied_seats: int = Field(ge=0)
    available_seats: int = Field(ge=0)


class ReadinessResponse(BaseModel):
    status: str
    database: str
