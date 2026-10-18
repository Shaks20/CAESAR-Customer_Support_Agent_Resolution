from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict, field_validator


class TicketCreate(BaseModel):
    order_id: str | None = None
    complaint: str = Field(min_length=8, max_length=4000)

    @field_validator("complaint")
    @classmethod
    def complaint_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Complaint cannot be blank.")
        return value


class MessageCreate(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    order_id: str | None = None

    @field_validator("message")
    @classmethod
    def message_must_contain_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be blank.")
        return value


class TicketMessageOut(BaseModel):
    id: str
    ticket_id: str
    sender: str
    message: str
    created_at: datetime


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    customer_id: str
    order_id: str | None
    complaint: str
    category: str
    status: str
    ticketing_sync_status: str = "pending"
    created_at: datetime


class RunOut(BaseModel):
    id: str
    ticket_id: str
    status: str
    summary: str | None = None
    proposal_id: str | None = None
    current_step: str = "created"
    last_error: str | None = None


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = Field(default=None, max_length=1000)


class ProposalOut(BaseModel):
    id: str
    action_type: str
    amount_minor: int
    currency: str
    rationale: str
    evidence: list[dict]
    policy_citations: list[dict]
    status: str


class AuditEventOut(BaseModel):
    id: str
    actor: str
    event_type: str
    details: dict
    created_at: datetime
