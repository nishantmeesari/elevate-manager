from uuid import UUID

from pydantic import BaseModel


class PassPurchaseRequest(BaseModel):
    pass_plan_id: UUID
    idempotency_key: str


class PaymentConfirmationRequest(BaseModel):
    outcome: str
