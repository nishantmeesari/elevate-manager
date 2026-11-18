from uuid import UUID

from pydantic import BaseModel, Field


class CustomerRegisterRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    phone_e164: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    email: str
    password: str = Field(min_length=12)


class CustomerLoginRequest(BaseModel):
    email: str
    password: str


class AdminBootstrapRequest(BaseModel):
    bootstrap_token: str = Field(min_length=1)
    email: str
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12)


class StaffLoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PrincipalResponse(BaseModel):
    id: UUID
    principal_type: str
    role: str | None = None
