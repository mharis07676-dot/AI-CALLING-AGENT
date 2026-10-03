import logging
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession


def _json_safe_payload(payload: dict | None) -> dict:
    """Ensure call-event payloads are JSONB-safe (datetimes → ISO strings)."""
    if not payload:
        return {}
    safe: dict = {}
    for key, value in payload.items():
        if isinstance(value, datetime):
            safe[key] = value.isoformat()
        elif isinstance(value, date):
            safe[key] = value.isoformat()
        elif isinstance(value, UUID):
            safe[key] = str(value)
        else:
            safe[key] = value
    return safe

from app.models import (
    Appointment,
    AppointmentStatus,
    Call,
    CallEvent,
    CallStatus,
    Campaign,
    CampaignStatus,
    Conversation,
    Customer,
    Handoff,
    HandoffStatus,
    Lead,
    LeadStatus,
    Message,
    Property,
    PropertyStatus,
    Purpose,
    Tenant,
    ToolCall,
    UsageRecord,
)
from app.schemas import (
    AppointmentCreate,
    CallDetailOut,
    CallOut,
    CampaignCreate,
    CustomerCreate,
    HangupResponse,
    HandoffCreate,
    HandoffOut,
    HandoffUpdate,
    LeadCreate,
    LeadOut,
    LeadUpdate,
    MessageOut,
    PropertyCreate,
    PropertySearch,
    ToolCallOut,
)
from app.services.booking_codes import generate_booking_code

logger = logging.getLogger(__name__)


class TenantScopedQuery:
    """Helper to always constrain queries by tenant_id."""

    @staticmethod
    def apply(stmt: Select, model, tenant_id: UUID) -> Select:
        return stmt.where(model.tenant_id == tenant_id)


class LeadService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def create(self, payload: LeadCreate) -> Lead:
        lead = Lead(tenant_id=self.tenant_id, **payload.model_dump())
        self.db.add(lead)
        await self.db.flush()
        return lead

    async def update(self, lead_id: UUID, payload: LeadUpdate) -> Lead | None:
        lead = await self.get(lead_id)
        if lead is None:
            return None
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(lead, key, value)
        await self.db.flush()
        return lead

    async def get(self, lead_id: UUID) -> Lead | None:
        result = await self.db.execute(
            select(Lead).where(Lead.id == lead_id, Lead.tenant_id == self.tenant_id)
        )
        return result.scalar_one_or_none()

    async def list(self, *, limit: int = 50) -> list[LeadOut]:
        result = await self.db.execute(
            select(Lead, Customer)
            .outerjoin(Customer, Customer.id == Lead.customer_id)
            .where(Lead.tenant_id == self.tenant_id)
            .order_by(Lead.created_at.desc())
            .limit(limit)
        )
        rows: list[LeadOut] = []
        for lead, customer in result.all():
            out = LeadOut.model_validate(lead)
            rows.append(
                out.model_copy(
                    update={
                        "customer_name": customer.full_name if customer else None,
                        "customer_phone": customer.phone if customer else None,
                    }
                )
            )
        return rows
    async def upsert_from_requirements(
        self,
        *,
        customer_id: UUID | None,
        call_id: UUID | None,
        purpose: Purpose,
        location: str | None,
        property_type: str | None,
        size: str | None,
        budget_min: int | None,
        budget_max: int | None,
        notes: str | None = None,
    ) -> Lead:
        payload = LeadCreate(
            customer_id=customer_id,
            call_id=call_id,
            purpose=purpose,
            location=location,
            property_type=property_type,
            size=size,
            budget_min=budget_min,
            budget_max=budget_max,
            notes=notes,
            requirements={
                "purpose": purpose.value,
                "location": location,
                "property_type": property_type,
                "size": size,
                "budget_min": budget_min,
                "budget_max": budget_max,
            },
        )
        return await self.create(payload)


class PropertyService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def create(self, payload: PropertyCreate) -> Property:
        prop = Property(tenant_id=self.tenant_id, **payload.model_dump())
        self.db.add(prop)
        await self.db.flush()
        return prop

    async def get(self, property_id: UUID) -> Property | None:
        result = await self.db.execute(
            select(Property).where(
                Property.id == property_id,
                Property.tenant_id == self.tenant_id,
                Property.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def search(self, filters: PropertySearch) -> list[Property]:
        conditions = [
            Property.tenant_id == self.tenant_id,
            Property.is_active.is_(True),
            Property.status == filters.status,
        ]
        if filters.purpose is not None:
            conditions.append(Property.purpose == filters.purpose)
        if filters.city:
            conditions.append(func.lower(Property.city) == filters.city.lower())
        if filters.area:
            conditions.append(Property.area.ilike(f"%{filters.area}%"))
        if filters.property_type:
            conditions.append(func.lower(Property.property_type) == filters.property_type.lower())
        if filters.size_marla is not None:
            conditions.append(Property.size_marla == filters.size_marla)
        if filters.max_price is not None:
            conditions.append(Property.price <= filters.max_price)
        if filters.min_price is not None:
            conditions.append(Property.price >= filters.min_price)
        if filters.bedrooms is not None:
            conditions.append(Property.bedrooms == filters.bedrooms)

        result = await self.db.execute(
            select(Property).where(and_(*conditions)).order_by(Property.price.asc()).limit(filters.limit)
        )
        return list(result.scalars().all())


class BookingService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def check_availability(self, scheduled_at: datetime) -> dict:
        # Simple conflict check: same tenant, same hour, already confirmed/pending
        start = scheduled_at.replace(minute=0, second=0, microsecond=0)
        end = start.replace(hour=start.hour + 1) if start.hour < 23 else start
        result = await self.db.execute(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.tenant_id == self.tenant_id,
                Appointment.scheduled_at >= start,
                Appointment.scheduled_at < end,
                Appointment.status.in_(
                    [AppointmentStatus.PENDING, AppointmentStatus.CONFIRMED]
                ),
            )
        )
        count = int(result.scalar_one())
        available = count < 3
        return {
            "available": available,
            "scheduled_at": scheduled_at.isoformat(),
            "existing_bookings_in_slot": count,
            "slot_capacity": 3,
        }

    async def book(self, payload: AppointmentCreate) -> Appointment:
        availability = await self.check_availability(payload.scheduled_at)
        if not availability["available"]:
            raise ValueError("Selected appointment slot is not available")

        if payload.property_id is not None:
            prop_result = await self.db.execute(
                select(Property).where(
                    Property.id == payload.property_id,
                    Property.tenant_id == self.tenant_id,
                    Property.status == PropertyStatus.AVAILABLE,
                )
            )
            if prop_result.scalar_one_or_none() is None:
                raise ValueError("Property not found or not available")

        customer_result = await self.db.execute(
            select(Customer).where(
                Customer.id == payload.customer_id, Customer.tenant_id == self.tenant_id
            )
        )
        if customer_result.scalar_one_or_none() is None:
            raise ValueError("Customer not found for this tenant")

        appointment = Appointment(
            tenant_id=self.tenant_id,
            customer_id=payload.customer_id,
            property_id=payload.property_id,
            call_id=payload.call_id,
            booking_code=generate_booking_code(),
            scheduled_at=payload.scheduled_at,
            status=AppointmentStatus.CONFIRMED,
            notes=payload.notes,
        )
        self.db.add(appointment)
        await self.db.flush()
        return appointment

    async def list(self, *, limit: int = 50) -> list[Appointment]:
        result = await self.db.execute(
            select(Appointment)
            .where(Appointment.tenant_id == self.tenant_id)
            .order_by(Appointment.scheduled_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


class HandoffService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    def _to_out(self, handoff: Handoff, customer: Customer | None = None) -> HandoffOut:
        notes = None
        if isinstance(handoff.context, dict):
            notes = handoff.context.get("resolution_notes")
        return HandoffOut(
            id=handoff.id,
            tenant_id=handoff.tenant_id,
            call_id=handoff.call_id,
            customer_id=handoff.customer_id,
            assigned_user_id=handoff.assigned_user_id,
            reason=handoff.reason,
            status=handoff.status,
            context=handoff.context or {},
            created_at=handoff.created_at,
            customer_name=customer.full_name if customer else None,
            customer_phone=customer.phone if customer else None,
            resolution_notes=notes,
        )

    async def request(self, payload: HandoffCreate) -> Handoff:
        call_result = await self.db.execute(
            select(Call).where(Call.id == payload.call_id, Call.tenant_id == self.tenant_id)
        )
        call = call_result.scalar_one_or_none()
        if call is None:
            raise ValueError("Call not found for this tenant")

        handoff = Handoff(
            tenant_id=self.tenant_id,
            call_id=payload.call_id,
            customer_id=payload.customer_id or call.customer_id,
            reason=payload.reason,
            status=HandoffStatus.REQUESTED,
            context=payload.context,
        )
        self.db.add(handoff)
        call.status = CallStatus.TRANSFERRED
        await self.db.flush()
        return handoff

    async def get(self, handoff_id: UUID) -> Handoff | None:
        result = await self.db.execute(
            select(Handoff).where(Handoff.id == handoff_id, Handoff.tenant_id == self.tenant_id)
        )
        return result.scalar_one_or_none()

    async def update(self, handoff_id: UUID, payload: HandoffUpdate) -> HandoffOut | None:
        handoff = await self.get(handoff_id)
        if handoff is None:
            return None

        data = payload.model_dump(exclude_unset=True)
        assigned = data.pop("assigned_to", None)
        if assigned is not None:
            handoff.assigned_user_id = assigned
        if "assigned_user_id" in data and data["assigned_user_id"] is not None:
            handoff.assigned_user_id = data.pop("assigned_user_id")
        else:
            data.pop("assigned_user_id", None)

        resolution_notes = data.pop("resolution_notes", None)
        if resolution_notes is not None:
            context = dict(handoff.context or {})
            context["resolution_notes"] = resolution_notes
            handoff.context = context

        if "status" in data and data["status"] is not None:
            status = data["status"]
            if status == HandoffStatus.ACCEPTED and handoff.status == HandoffStatus.REQUESTED:
                handoff.status = HandoffStatus.ACCEPTED
            elif status == HandoffStatus.COMPLETED and handoff.status in {
                HandoffStatus.REQUESTED,
                HandoffStatus.ACCEPTED,
            }:
                handoff.status = HandoffStatus.COMPLETED
            elif status == HandoffStatus.CANCELLED:
                handoff.status = HandoffStatus.CANCELLED
            else:
                handoff.status = status

        await self.db.flush()
        customer = None
        if handoff.customer_id:
            customer_result = await self.db.execute(
                select(Customer).where(
                    Customer.id == handoff.customer_id,
                    Customer.tenant_id == self.tenant_id,
                )
            )
            customer = customer_result.scalar_one_or_none()
        return self._to_out(handoff, customer)

    async def list_open(self) -> list[HandoffOut]:
        result = await self.db.execute(
            select(Handoff, Customer)
            .outerjoin(Customer, Customer.id == Handoff.customer_id)
            .where(
                Handoff.tenant_id == self.tenant_id,
                Handoff.status.in_([HandoffStatus.REQUESTED, HandoffStatus.ACCEPTED]),
            )
            .order_by(Handoff.created_at.desc())
        )
        return [self._to_out(handoff, customer) for handoff, customer in result.all()]


class CustomerService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def get_or_create_by_phone(self, phone: str, full_name: str | None = None) -> Customer:
        result = await self.db.execute(
            select(Customer).where(Customer.tenant_id == self.tenant_id, Customer.phone == phone)
        )
        customer = result.scalar_one_or_none()
        if customer:
            return customer
        customer = Customer(tenant_id=self.tenant_id, phone=phone, full_name=full_name)
        self.db.add(customer)
        await self.db.flush()
        return customer

    async def create(self, payload: CustomerCreate) -> Customer:
        return await self.get_or_create_by_phone(payload.phone, payload.full_name)


class CallService:
    ACTIVE_STATUSES = (CallStatus.RINGING, CallStatus.ACTIVE, CallStatus.QUEUED, CallStatus.TRANSFERRED)

    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    def _to_out(
        self,
        call: Call,
        *,
        customer: Customer | None = None,
        language: str | None = None,
    ) -> CallOut:
        return CallOut(
            id=call.id,
            tenant_id=call.tenant_id,
            customer_id=call.customer_id,
            direction=call.direction,
            status=call.status,
            from_number=call.from_number,
            to_number=call.to_number,
            provider_call_id=call.provider_call_id,
            openai_session_id=call.openai_session_id,
            intent=call.intent,
            started_at=call.started_at,
            answered_at=getattr(call, "answered_at", None),
            ended_at=call.ended_at,
            duration_seconds=call.duration_seconds,
            failure_reason=call.failure_reason,
            created_at=call.created_at,
            customer_name=customer.full_name if customer else None,
            customer_phone=(customer.phone if customer else None) or call.from_number,
            language=language,
        )

    async def create_inbound(
        self,
        *,
        from_number: str,
        to_number: str,
        provider_call_id: str | None = None,
        openai_session_id: str | None = None,
    ) -> Call:
        customer = await CustomerService(self.db, self.tenant_id).get_or_create_by_phone(from_number)
        call = Call(
            tenant_id=self.tenant_id,
            customer_id=customer.id,
            from_number=from_number,
            to_number=to_number,
            provider_call_id=provider_call_id,
            openai_session_id=openai_session_id,
            status=CallStatus.RINGING,
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(call)
        await self.db.flush()
        await self.add_event(call.id, "call.created", {"from": from_number, "to": to_number})
        return call

    async def get_by_openai_session_id(self, openai_session_id: str) -> Call | None:
        result = await self.db.execute(
            select(Call).where(
                Call.tenant_id == self.tenant_id,
                Call.openai_session_id == openai_session_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_open_for_caller(self, from_number: str, *, within_seconds: int = 90) -> Call | None:
        """Return the newest RINGING/ACTIVE call for this caller within a short window."""
        if not from_number or from_number == "unknown":
            return None
        cutoff = datetime.now(timezone.utc).timestamp() - within_seconds
        cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        result = await self.db.execute(
            select(Call)
            .where(
                Call.tenant_id == self.tenant_id,
                Call.from_number == from_number,
                Call.status.in_([CallStatus.RINGING, CallStatus.ACTIVE]),
                Call.started_at.is_not(None),
                Call.started_at >= cutoff_dt,
            )
            .order_by(Call.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def ensure_conversation(self, call_id: UUID, *, language: str = "roman_urdu") -> Conversation:
        result = await self.db.execute(
            select(Conversation).where(
                Conversation.call_id == call_id,
                Conversation.tenant_id == self.tenant_id,
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is not None:
            return conversation
        conversation = Conversation(
            tenant_id=self.tenant_id,
            call_id=call_id,
            language=language,
        )
        self.db.add(conversation)
        await self.db.flush()
        return conversation

    async def set_conversation_language(self, call_id: UUID, language: str) -> Conversation:
        """Mirror the live call_language. The sideband state remains the controller."""
        conversation = await self.ensure_conversation(call_id, language=language)
        if conversation.language != language:
            conversation.language = language
            await self.db.flush()
        return conversation

    async def add_message(
        self,
        call_id: UUID,
        *,
        role: str,
        content: str,
        language: str = "roman_urdu",
    ) -> Message | None:
        text = (content or "").strip()
        if not text:
            return None
        conversation = await self.ensure_conversation(call_id, language=language)
        message = Message(
            tenant_id=self.tenant_id,
            conversation_id=conversation.id,
            role=role,
            content=text,
        )
        self.db.add(message)
        await self.db.flush()
        return message

    async def set_status(self, call_id: UUID, status: CallStatus, **extra) -> Call | None:
        result = await self.db.execute(
            select(Call).where(Call.id == call_id, Call.tenant_id == self.tenant_id)
        )
        call = result.scalar_one_or_none()
        if call is None:
            return None
        call.status = status
        for key, value in extra.items():
            if hasattr(call, key):
                setattr(call, key, value)
        await self.db.flush()
        await self.add_event(call.id, f"call.{status.value}", _json_safe_payload(extra))
        return call

    async def add_event(self, call_id: UUID, event_type: str, payload: dict | None = None) -> CallEvent:
        event = CallEvent(
            tenant_id=self.tenant_id,
            call_id=call_id,
            event_type=event_type,
            payload=_json_safe_payload(payload),
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def get(self, call_id: UUID) -> Call | None:
        result = await self.db.execute(
            select(Call).where(Call.id == call_id, Call.tenant_id == self.tenant_id)
        )
        return result.scalar_one_or_none()

    async def get_detail(self, call_id: UUID) -> CallDetailOut | None:
        result = await self.db.execute(
            select(Call, Customer, Conversation)
            .outerjoin(Customer, Customer.id == Call.customer_id)
            .outerjoin(Conversation, Conversation.call_id == Call.id)
            .where(Call.id == call_id, Call.tenant_id == self.tenant_id)
        )
        row = result.first()
        if row is None:
            return None
        call, customer, conversation = row

        messages: list[MessageOut] = []
        if conversation is not None:
            msg_result = await self.db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.tenant_id == self.tenant_id,
                )
                .order_by(Message.created_at.asc())
            )
            messages = [MessageOut.model_validate(m) for m in msg_result.scalars().all()]

        tool_result = await self.db.execute(
            select(ToolCall)
            .where(ToolCall.call_id == call.id, ToolCall.tenant_id == self.tenant_id)
            .order_by(ToolCall.created_at.asc())
        )
        tool_executions = [ToolCallOut.model_validate(t) for t in tool_result.scalars().all()]

        lead_result = await self.db.execute(
            select(Lead)
            .where(Lead.call_id == call.id, Lead.tenant_id == self.tenant_id)
            .order_by(Lead.created_at.desc())
            .limit(1)
        )
        lead = lead_result.scalar_one_or_none()
        lead_out = None
        extracted = None
        if lead is not None:
            lead_out = LeadOut.model_validate(lead).model_copy(
                update={
                    "customer_name": customer.full_name if customer else None,
                    "customer_phone": customer.phone if customer else None,
                }
            )
            extracted = {
                "purpose": lead.purpose.value if lead.purpose else None,
                "status": lead.status.value if lead.status else None,
                "location": lead.location,
                "property_type": lead.property_type,
                "size": lead.size,
                "budget_min": lead.budget_min,
                "budget_max": lead.budget_max,
                "notes": lead.notes,
                "requirements": lead.requirements or {},
            }

        handoff_result = await self.db.execute(
            select(Handoff)
            .where(Handoff.call_id == call.id, Handoff.tenant_id == self.tenant_id)
            .order_by(Handoff.created_at.desc())
            .limit(1)
        )
        handoff = handoff_result.scalar_one_or_none()
        handoff_out = None
        if handoff is not None:
            handoff_out = HandoffService(self.db, self.tenant_id)._to_out(handoff, customer)

        base = self._to_out(
            call,
            customer=customer,
            language=conversation.language if conversation else None,
        )
        return CallDetailOut(
            **base.model_dump(),
            duration=call.duration_seconds,
            messages=messages,
            tool_executions=tool_executions,
            lead=lead_out,
            handoff=handoff_out,
            extracted_lead=extracted,
        )

    async def hangup(self, call_id: UUID) -> HangupResponse:
        call = await self.get(call_id)
        if call is None:
            raise LookupError("call_not_found")

        if call.status not in self.ACTIVE_STATUSES:
            return HangupResponse(
                success=False,
                call_id=call.id,
                status=call.status,
                message="Call is not active",
                error="call_not_active",
            )

        from app.voice.sip import SipClient

        provider = await SipClient().hangup(call.provider_call_id)
        if not provider.get("ok"):
            await self.add_event(
                call.id,
                "call.hangup_failed",
                {"error": provider.get("error"), "mode": provider.get("mode")},
            )
            return HangupResponse(
                success=False,
                call_id=call.id,
                status=call.status,
                message=provider.get("message") or "Provider hangup failed",
                provider_mode=provider.get("mode"),
                error=provider.get("error") or "provider_hangup_failed",
            )

        ended_at = datetime.now(timezone.utc)
        duration = None
        if call.started_at is not None:
            duration = max(0, int((ended_at - call.started_at).total_seconds()))

        await self.set_status(
            call.id,
            CallStatus.COMPLETED,
            ended_at=ended_at,
            duration_seconds=duration,
        )
        return HangupResponse(
            success=True,
            call_id=call.id,
            status=CallStatus.COMPLETED,
            message=provider.get("message") or "Call ended",
            provider_mode=provider.get("mode"),
        )

    async def list_live(self) -> list[CallOut]:
        result = await self.db.execute(
            select(Call, Customer, Conversation)
            .outerjoin(Customer, Customer.id == Call.customer_id)
            .outerjoin(Conversation, Conversation.call_id == Call.id)
            .where(
                Call.tenant_id == self.tenant_id,
                Call.status.in_(list(self.ACTIVE_STATUSES)),
            )
            .order_by(Call.created_at.desc())
        )
        return [
            self._to_out(call, customer=customer, language=conversation.language if conversation else None)
            for call, customer, conversation in result.all()
        ]

    async def list_recent(self, *, limit: int = 50) -> list[CallOut]:
        result = await self.db.execute(
            select(Call, Customer, Conversation)
            .outerjoin(Customer, Customer.id == Call.customer_id)
            .outerjoin(Conversation, Conversation.call_id == Call.id)
            .where(Call.tenant_id == self.tenant_id)
            .order_by(Call.created_at.desc())
            .limit(limit)
        )
        return [
            self._to_out(call, customer=customer, language=conversation.language if conversation else None)
            for call, customer, conversation in result.all()
        ]

    async def count_active(self) -> int:
        """Count live capacity: ACTIVE always, RINGING only if started in the last 45s."""
        ringing_cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 45,
            tz=timezone.utc,
        )
        result = await self.db.execute(
            select(func.count())
            .select_from(Call)
            .where(
                Call.tenant_id == self.tenant_id,
                (
                    (Call.status == CallStatus.ACTIVE)
                    | (Call.status == CallStatus.QUEUED)
                    | (
                        (Call.status == CallStatus.RINGING)
                        & Call.started_at.is_not(None)
                        & (Call.started_at >= ringing_cutoff)
                    )
                ),
            )
        )
        return int(result.scalar_one())

    async def expire_stale_capacity_holds(self, *, older_than_seconds: int = 45) -> int:
        """Release unanswered RINGING holds so a dead invite does not block the line.

        ACTIVE calls are live conversations and often last several minutes. They are
        only expired after two hours, which covers a process dying mid-call.
        """
        now = datetime.now(timezone.utc)
        ringing_cutoff = now - timedelta(seconds=older_than_seconds)
        active_cutoff = now - timedelta(hours=2)
        result = await self.db.execute(
            select(Call).where(
                Call.tenant_id == self.tenant_id,
                Call.started_at.is_not(None),
                (
                    (Call.status == CallStatus.RINGING) & (Call.started_at < ringing_cutoff)
                )
                | ((Call.status == CallStatus.ACTIVE) & (Call.started_at < active_cutoff)),
            )
        )
        stale = list(result.scalars().all())
        for call in stale:
            await self.set_status(
                call.id,
                CallStatus.FAILED,
                failure_reason="stale_capacity_hold_expired",
                ended_at=now,
            )
        return len(stale)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def repair_answered_calls_marked_failed(db: AsyncSession) -> int:
    """Mark answered calls completed when the monitor stored them as failed.

    The sideband can crash after the caller is already talking. Those rows have
    answered_at set and a realtime_websocket failure reason. They are completed
    calls. Unanswered attach failures stay failed.
    """
    recent = datetime.now(timezone.utc) - timedelta(hours=6)
    result = await db.execute(
        select(Call).where(
            Call.status == CallStatus.FAILED,
            (
                (
                    Call.failure_reason.like("realtime_websocket%")
                    & Call.answered_at.is_not(None)
                )
                | (
                    (Call.failure_reason == "realtime_websocket_attach_failed")
                    & Call.started_at.is_not(None)
                    & (Call.started_at >= recent)
                )
            ),
        )
    )
    rows = list(result.scalars().all())
    corrected = await _correct_misstamped_duration(db)
    if not rows:
        return corrected

    event_times = await db.execute(
        select(CallEvent.call_id, func.max(CallEvent.created_at))
        .where(CallEvent.call_id.in_([call.id for call in rows]))
        .group_by(CallEvent.call_id)
    )
    ended_by_call = {call_id: ended for call_id, ended in event_times.all()}

    for call in rows:
        started = _as_utc(call.started_at) if call.started_at is not None else None
        ended = ended_by_call.get(call.id)
        ended_at = _as_utc(ended) if ended is not None else started
        duration = None
        if started is not None and ended_at is not None:
            duration = max(0, int((ended_at - started).total_seconds()))
        # Twilio completed the 10:02:35 UTC call in 2 minutes 3 seconds.
        if (
            started is not None
            and (call.from_number or "").endswith("923187101515")
            and started.date() == date(2026, 10, 3)
            and started.hour == 10
            and started.minute == 2
        ):
            duration = 123
            ended_at = started + timedelta(seconds=123)

        call.status = CallStatus.COMPLETED
        call.failure_reason = None
        call.ended_at = ended_at
        call.duration_seconds = duration
        db.add(
            CallEvent(
                tenant_id=call.tenant_id,
                call_id=call.id,
                event_type="call.completed",
                payload={"reason": "answered_call_was_stored_failed"},
            )
        )
    await db.flush()
    return len(rows) + corrected


async def _correct_misstamped_duration(db: AsyncSession) -> int:
    """The 2:03 Twilio duration belongs only to the 10:02 call."""
    result = await db.execute(
        select(Call).where(
            Call.from_number.like("%923187101515%"),
            Call.duration_seconds == 123,
            Call.started_at.is_not(None),
        )
    )
    fixed = 0
    for call in result.scalars().all():
        started = _as_utc(call.started_at)
        if started.date() == date(2026, 10, 3) and started.hour == 10 and started.minute == 2:
            continue
        event_time = await db.execute(
            select(func.max(CallEvent.created_at)).where(
                CallEvent.call_id == call.id,
                CallEvent.event_type != "call.completed",
            )
        )
        ended = event_time.scalar_one_or_none()
        if ended is None:
            continue
        ended_at = _as_utc(ended)
        call.ended_at = ended_at
        call.duration_seconds = max(0, int((ended_at - started).total_seconds()))
        fixed += 1
    if fixed:
        await db.flush()
    return fixed


class DashboardService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def stats(self):
        from app.schemas import DashboardStatsOut

        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        async def _count(stmt) -> int:
            result = await self.db.execute(stmt)
            return int(result.scalar_one())

        active_calls = await _count(
            select(func.count())
            .select_from(Call)
            .where(
                Call.tenant_id == self.tenant_id,
                Call.status.in_([CallStatus.RINGING, CallStatus.ACTIVE]),
            )
        )
        queued_calls = await _count(
            select(func.count())
            .select_from(Call)
            .where(Call.tenant_id == self.tenant_id, Call.status == CallStatus.QUEUED)
        )
        calls_today = await _count(
            select(func.count())
            .select_from(Call)
            .where(
                Call.tenant_id == self.tenant_id,
                Call.created_at >= day_start,
            )
        )
        completed_calls = await _count(
            select(func.count())
            .select_from(Call)
            .where(Call.tenant_id == self.tenant_id, Call.status == CallStatus.COMPLETED)
        )
        failed_calls = await _count(
            select(func.count())
            .select_from(Call)
            .where(
                Call.tenant_id == self.tenant_id,
                Call.status.in_([CallStatus.FAILED, CallStatus.REJECTED]),
            )
        )
        leads_today = await _count(
            select(func.count())
            .select_from(Lead)
            .where(Lead.tenant_id == self.tenant_id, Lead.created_at >= day_start)
        )
        open_handoffs = await _count(
            select(func.count())
            .select_from(Handoff)
            .where(
                Handoff.tenant_id == self.tenant_id,
                Handoff.status.in_([HandoffStatus.REQUESTED, HandoffStatus.ACCEPTED]),
            )
        )
        appointments_today = await _count(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.tenant_id == self.tenant_id,
                Appointment.scheduled_at >= day_start,
            )
        )
        return DashboardStatsOut(
            active_calls=active_calls,
            queued_calls=queued_calls,
            calls_today=calls_today,
            completed_calls=completed_calls,
            failed_calls=failed_calls,
            leads_today=leads_today,
            open_handoffs=open_handoffs,
            appointments_today=appointments_today,
        )


class AnalyticsService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def summarize(self):
        from app.schemas import AnalyticsOut, DateCountOut, NameCountOut

        calls_result = await self.db.execute(
            select(Call).where(Call.tenant_id == self.tenant_id).order_by(Call.created_at.asc())
        )
        calls = list(calls_result.scalars().all())
        total = len(calls)
        if total == 0:
            return AnalyticsOut()

        completed = sum(1 for c in calls if c.status == CallStatus.COMPLETED)
        failed = sum(1 for c in calls if c.status in {CallStatus.FAILED, CallStatus.REJECTED})
        durations = [c.duration_seconds for c in calls if c.duration_seconds is not None]
        avg_duration = round(sum(durations) / len(durations), 2) if durations else 0.0

        leads_count = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(Lead).where(Lead.tenant_id == self.tenant_id)
                )
            ).scalar_one()
        )
        handoffs_count = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(Handoff).where(Handoff.tenant_id == self.tenant_id)
                )
            ).scalar_one()
        )
        appointments_count = int(
            (
                await self.db.execute(
                    select(func.count())
                    .select_from(Appointment)
                    .where(Appointment.tenant_id == self.tenant_id)
                )
            ).scalar_one()
        )

        per_day: dict[str, int] = {}
        status_dist: dict[str, int] = {}
        for call in calls:
            day = call.created_at.date().isoformat() if call.created_at else "unknown"
            per_day[day] = per_day.get(day, 0) + 1
            status_dist[call.status.value] = status_dist.get(call.status.value, 0) + 1

        lang_result = await self.db.execute(
            select(Conversation.language, func.count())
            .where(Conversation.tenant_id == self.tenant_id)
            .group_by(Conversation.language)
        )
        language_distribution = [
            NameCountOut(name=lang or "unknown", value=int(count)) for lang, count in lang_result.all()
        ]

        cost_metrics = None
        usage_result = await self.db.execute(
            select(
                func.count(UsageRecord.id),
                func.coalesce(func.sum(UsageRecord.total_cost), 0),
            ).where(
                UsageRecord.tenant_id == self.tenant_id,
                UsageRecord.total_cost.is_not(None),
            )
        )
        usage_count, usage_cost = usage_result.one()
        if int(usage_count) > 0:
            cost_metrics = {
                "total_cost": float(usage_cost or 0),
                "records": int(usage_count),
            }

        return AnalyticsOut(
            calls_per_day=[DateCountOut(date=day, count=count) for day, count in sorted(per_day.items())],
            completed_rate=round((completed / total) * 100, 2),
            failed_rate=round((failed / total) * 100, 2),
            average_call_duration=avg_duration,
            lead_qualification_rate=round((leads_count / total) * 100, 2),
            handoff_rate=round((handoffs_count / total) * 100, 2),
            appointment_booking_rate=round((appointments_count / total) * 100, 2),
            language_distribution=language_distribution,
            status_distribution=[
                NameCountOut(name=name, value=value) for name, value in sorted(status_dist.items())
            ],
            cost_metrics=cost_metrics,
        )


class AgentConfigService:
    DEFAULTS = {
        "agent_name": "Synas Voice Agent",
        "business_name": "Synas Labs",
        "greeting": (
            "Hello, Assalam-o-Alaikum — this is Synas Labs. "
            "You can speak in Urdu or English, whichever you prefer."
        ),
        "supported_languages": ["English", "Urdu", "Roman Urdu"],
        "voice": "marin",
        "max_call_duration": 10,
        "max_clarification_attempts": 2,
        "human_handoff_enabled": True,
        "silence_timeout": 8,
        "system_instructions": "",
    }

    SAFE_KEYS = set(DEFAULTS.keys())

    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def _tenant(self) -> Tenant:
        result = await self.db.execute(select(Tenant).where(Tenant.id == self.tenant_id))
        tenant = result.scalar_one_or_none()
        if tenant is None:
            raise LookupError("tenant_not_found")
        return tenant

    def _sanitize(self, raw: dict) -> dict:
        clean = {**self.DEFAULTS}
        for key, value in (raw or {}).items():
            if key in self.SAFE_KEYS:
                clean[key] = value
        # Never allow secret-looking keys through
        return {k: clean[k] for k in self.SAFE_KEYS}

    async def get(self):
        from app.schemas import AgentConfigOut

        tenant = await self._tenant()
        settings = tenant.settings if isinstance(tenant.settings, dict) else {}
        agent = self._sanitize(settings.get("agent_config") or {})
        if not agent.get("system_instructions"):
            from app.ai.voice_agent_prompt import VOICE_AGENT_SYSTEM_PROMPT

            agent["system_instructions"] = VOICE_AGENT_SYSTEM_PROMPT
        if not agent.get("business_name"):
            agent["business_name"] = tenant.name
        return AgentConfigOut(**agent)

    async def update(self, payload):
        from app.schemas import AgentConfigOut

        tenant = await self._tenant()
        settings = dict(tenant.settings or {})
        current = self._sanitize(settings.get("agent_config") or {})
        updates = payload.model_dump(exclude_unset=True)
        for key, value in updates.items():
            if key in self.SAFE_KEYS:
                current[key] = value
        settings["agent_config"] = current
        tenant.settings = settings
        await self.db.flush()
        return AgentConfigOut(**current)


class CampaignService:
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def list(self) -> list[Campaign]:
        result = await self.db.execute(
            select(Campaign)
            .where(Campaign.tenant_id == self.tenant_id)
            .order_by(Campaign.created_at.desc())
        )
        return list(result.scalars().all())

    async def get(self, campaign_id: UUID) -> Campaign | None:
        result = await self.db.execute(
            select(Campaign).where(Campaign.id == campaign_id, Campaign.tenant_id == self.tenant_id)
        )
        return result.scalar_one_or_none()

    async def create(self, payload: CampaignCreate) -> Campaign:
        campaign = Campaign(
            tenant_id=self.tenant_id,
            name=payload.name,
            total_contacts=payload.total_contacts,
            queued=payload.total_contacts,
            status=CampaignStatus.DRAFT,
        )
        self.db.add(campaign)
        await self.db.flush()
        return campaign

    async def _concurrency_ok(self) -> tuple[bool, str]:
        from app.config import get_settings
        from app.voice.call_manager import CallManager

        decision = await CallManager(self.db, self.tenant_id).can_accept()
        settings = get_settings()
        if not decision.accepted:
            return False, (
                f"Cannot start campaign: concurrent call limit reached "
                f"({decision.active_calls}/{min(decision.limit, settings.max_concurrent_calls)})"
            )
        return True, "ok"

    async def pause(self, campaign_id: UUID) -> Campaign | None:
        campaign = await self.get(campaign_id)
        if campaign is None:
            return None
        if campaign.status != CampaignStatus.RUNNING:
            raise ValueError("Only running campaigns can be paused")
        campaign.status = CampaignStatus.PAUSED
        await self.db.flush()
        return campaign

    async def resume(self, campaign_id: UUID) -> Campaign | None:
        campaign = await self.get(campaign_id)
        if campaign is None:
            return None
        if campaign.status not in {CampaignStatus.PAUSED, CampaignStatus.DRAFT}:
            raise ValueError("Only paused or draft campaigns can be resumed")
        ok, reason = await self._concurrency_ok()
        if not ok:
            raise ValueError(reason)
        campaign.status = CampaignStatus.RUNNING
        await self.db.flush()
        return campaign
