from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    AppointmentStatus,
    CallDirection,
    CallStatus,
    CampaignStatus,
    HandoffStatus,
    LeadStatus,
    PropertyStatus,
    Purpose,
    UserRole,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TenantCreate(BaseModel):
    name: str
    slug: str
    max_concurrent_calls: int = 10


class TenantOut(ORMModel):
    id: UUID
    name: str
    slug: str
    is_active: bool
    max_concurrent_calls: int


class UserCreate(BaseModel):
    email: str
    full_name: str
    password: str
    role: UserRole = UserRole.AGENT


class UserOut(ORMModel):
    id: UUID
    tenant_id: UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    email: str
    password: str
    tenant_slug: str


class CustomerCreate(BaseModel):
    phone: str
    full_name: str | None = None
    email: str | None = None
    language: str = "roman_urdu"


class CustomerOut(ORMModel):
    id: UUID
    tenant_id: UUID
    phone: str
    full_name: str | None
    email: str | None
    language: str


class LeadCreate(BaseModel):
    customer_id: UUID | None = None
    call_id: UUID | None = None
    purpose: Purpose = Purpose.GENERAL
    location: str | None = None
    property_type: str | None = None
    size: str | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    notes: str | None = None
    requirements: dict = Field(default_factory=dict)


class LeadUpdate(BaseModel):
    purpose: Purpose | None = None
    status: LeadStatus | None = None
    location: str | None = None
    property_type: str | None = None
    size: str | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    notes: str | None = None
    requirements: dict | None = None


class LeadOut(ORMModel):
    id: UUID
    tenant_id: UUID
    customer_id: UUID | None
    call_id: UUID | None
    purpose: Purpose
    status: LeadStatus
    location: str | None
    property_type: str | None
    size: str | None
    budget_min: int | None
    budget_max: int | None
    notes: str | None
    requirements: dict
    created_at: datetime
    customer_name: str | None = None
    customer_phone: str | None = None


class PropertyCreate(BaseModel):
    title: str
    purpose: Purpose
    property_type: str
    city: str
    area: str
    size: str | None = None
    size_marla: float | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    price: int
    currency: str = "PKR"
    status: PropertyStatus = PropertyStatus.AVAILABLE
    description: str | None = None
    amenities: dict = Field(default_factory=dict)
    external_ref: str | None = None


class PropertySearch(BaseModel):
    purpose: Purpose | None = None
    city: str | None = None
    area: str | None = None
    property_type: str | None = None
    size_marla: float | None = None
    max_price: int | None = None
    min_price: int | None = None
    bedrooms: int | None = None
    status: PropertyStatus = PropertyStatus.AVAILABLE
    limit: int = Field(default=5, ge=1, le=20)


class PropertyOut(ORMModel):
    id: UUID
    tenant_id: UUID
    external_ref: str | None
    title: str
    purpose: Purpose
    property_type: str
    city: str
    area: str
    size: str | None
    size_marla: float | None
    bedrooms: int | None
    bathrooms: int | None
    price: int
    currency: str
    status: PropertyStatus
    description: str | None
    amenities: dict
    is_active: bool


class AppointmentCreate(BaseModel):
    customer_id: UUID
    property_id: UUID | None = None
    call_id: UUID | None = None
    scheduled_at: datetime
    notes: str | None = None


class AppointmentOut(ORMModel):
    id: UUID
    tenant_id: UUID
    customer_id: UUID
    property_id: UUID | None
    call_id: UUID | None
    booking_code: str
    scheduled_at: datetime
    status: AppointmentStatus
    notes: str | None


class CallCreate(BaseModel):
    from_number: str
    to_number: str
    direction: CallDirection = CallDirection.INBOUND
    customer_id: UUID | None = None
    provider_call_id: str | None = None


class CallOut(ORMModel):
    id: UUID
    tenant_id: UUID
    customer_id: UUID | None
    direction: CallDirection
    status: CallStatus
    from_number: str
    to_number: str
    provider_call_id: str | None
    openai_session_id: str | None
    intent: str | None
    started_at: datetime | None
    answered_at: datetime | None = None
    ended_at: datetime | None
    duration_seconds: int | None
    failure_reason: str | None
    created_at: datetime
    customer_name: str | None = None
    customer_phone: str | None = None
    language: str | None = None


class MessageOut(ORMModel):
    id: UUID
    role: str
    content: str
    created_at: datetime


class ToolCallOut(ORMModel):
    id: UUID
    tool_name: str
    arguments: dict
    result: dict
    success: bool
    error: str | None
    created_at: datetime


class HandoffCreate(BaseModel):
    call_id: UUID
    customer_id: UUID | None = None
    reason: str
    context: dict = Field(default_factory=dict)


class HandoffUpdate(BaseModel):
    status: HandoffStatus | None = None
    assigned_to: UUID | None = None
    assigned_user_id: UUID | None = None
    resolution_notes: str | None = None


class HandoffOut(ORMModel):
    id: UUID
    tenant_id: UUID
    call_id: UUID
    customer_id: UUID | None
    assigned_user_id: UUID | None
    reason: str
    status: HandoffStatus
    context: dict
    created_at: datetime
    customer_name: str | None = None
    customer_phone: str | None = None
    resolution_notes: str | None = None


class CallDetailOut(CallOut):
    duration: int | None = None
    messages: list[MessageOut] = Field(default_factory=list)
    tool_executions: list[ToolCallOut] = Field(default_factory=list)
    lead: LeadOut | None = None
    handoff: HandoffOut | None = None
    extracted_lead: dict | None = None


class HangupResponse(BaseModel):
    success: bool
    call_id: UUID
    status: CallStatus | None = None
    message: str
    provider_mode: str | None = None
    error: str | None = None


class DashboardStatsOut(BaseModel):
    active_calls: int
    queued_calls: int
    calls_today: int
    completed_calls: int
    failed_calls: int
    leads_today: int
    open_handoffs: int
    appointments_today: int


class AgentConfigOut(BaseModel):
    agent_name: str
    business_name: str
    greeting: str
    supported_languages: list[str]
    voice: str
    max_call_duration: int
    max_clarification_attempts: int
    human_handoff_enabled: bool
    silence_timeout: int
    system_instructions: str


class AgentConfigUpdate(BaseModel):
    agent_name: str | None = None
    business_name: str | None = None
    greeting: str | None = None
    supported_languages: list[str] | None = None
    voice: str | None = None
    max_call_duration: int | None = Field(default=None, ge=1, le=120)
    max_clarification_attempts: int | None = Field(default=None, ge=0, le=10)
    human_handoff_enabled: bool | None = None
    silence_timeout: int | None = Field(default=None, ge=1, le=120)
    system_instructions: str | None = None


class NameCountOut(BaseModel):
    name: str
    value: int


class DateCountOut(BaseModel):
    date: str
    count: int


class AnalyticsOut(BaseModel):
    calls_per_day: list[DateCountOut] = Field(default_factory=list)
    completed_rate: float = 0
    failed_rate: float = 0
    average_call_duration: float = 0
    lead_qualification_rate: float = 0
    handoff_rate: float = 0
    appointment_booking_rate: float = 0
    language_distribution: list[NameCountOut] = Field(default_factory=list)
    status_distribution: list[NameCountOut] = Field(default_factory=list)
    cost_metrics: dict | None = None


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    total_contacts: int = Field(default=0, ge=0)


class CampaignOut(ORMModel):
    id: UUID
    name: str
    total_contacts: int
    queued: int
    dialing: int
    in_progress: int
    completed: int
    no_answer: int
    failed: int
    interested: int
    opted_out: int
    created_at: datetime
    status: CampaignStatus


class ToolExecutionResult(BaseModel):
    success: bool
    tool_name: str
    data: dict = Field(default_factory=dict)
    error: str | None = None
    speakable_summary: str | None = None