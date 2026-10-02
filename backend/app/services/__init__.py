from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    AppointmentStatus,
    Call,
    CallEvent,
    CallStatus,
    Customer,
    Handoff,
    HandoffStatus,
    Lead,
    LeadStatus,
    Property,
    PropertyStatus,
    Purpose,
)
from app.schemas import (
    AppointmentCreate,
    CustomerCreate,
    HandoffCreate,
    LeadCreate,
    LeadUpdate,
    PropertyCreate,
    PropertySearch,
)
from app.services.booking_codes import generate_booking_code


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

    async def list(self, *, limit: int = 50) -> list[Lead]:
        result = await self.db.execute(
            select(Lead)
            .where(Lead.tenant_id == self.tenant_id)
            .order_by(Lead.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

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

    async def list_open(self) -> list[Handoff]:
        result = await self.db.execute(
            select(Handoff)
            .where(
                Handoff.tenant_id == self.tenant_id,
                Handoff.status.in_([HandoffStatus.REQUESTED, HandoffStatus.ACCEPTED]),
            )
            .order_by(Handoff.created_at.desc())
        )
        return list(result.scalars().all())


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
    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id

    async def create_inbound(
        self,
        *,
        from_number: str,
        to_number: str,
        provider_call_id: str | None = None,
    ) -> Call:
        customer = await CustomerService(self.db, self.tenant_id).get_or_create_by_phone(from_number)
        call = Call(
            tenant_id=self.tenant_id,
            customer_id=customer.id,
            from_number=from_number,
            to_number=to_number,
            provider_call_id=provider_call_id,
            status=CallStatus.RINGING,
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(call)
        await self.db.flush()
        await self.add_event(call.id, "call.created", {"from": from_number, "to": to_number})
        return call

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
        await self.add_event(call.id, f"call.{status.value}", extra)
        return call

    async def add_event(self, call_id: UUID, event_type: str, payload: dict | None = None) -> CallEvent:
        event = CallEvent(
            tenant_id=self.tenant_id,
            call_id=call_id,
            event_type=event_type,
            payload=payload or {},
        )
        self.db.add(event)
        await self.db.flush()
        return event

    async def list_live(self) -> list[Call]:
        result = await self.db.execute(
            select(Call)
            .where(
                Call.tenant_id == self.tenant_id,
                Call.status.in_(
                    [CallStatus.RINGING, CallStatus.ACTIVE, CallStatus.QUEUED, CallStatus.TRANSFERRED]
                ),
            )
            .order_by(Call.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_recent(self, *, limit: int = 50) -> list[Call]:
        result = await self.db.execute(
            select(Call)
            .where(Call.tenant_id == self.tenant_id)
            .order_by(Call.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_active(self) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(Call)
            .where(
                Call.tenant_id == self.tenant_id,
                Call.status.in_([CallStatus.RINGING, CallStatus.ACTIVE, CallStatus.QUEUED]),
            )
        )
        return int(result.scalar_one())
