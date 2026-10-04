from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Call, Customer, Purpose, ToolCall
from app.schemas import (
    AppointmentCreate,
    PropertySearch,
    ToolExecutionResult,
)
from app.services import BookingService, CustomerService, LeadService, PropertyService
from app.voice.handoff import prepare_handoff, strip_model_destination_args

logger = logging.getLogger(__name__)


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "search_properties",
        "description": "Search real inventory. Never invent results.",
        "parameters": {
            "type": "object",
            "properties": {
                "purpose": {"type": "string", "enum": ["rent", "buy", "sell", "visit", "general"]},
                "city": {"type": "string"},
                "area": {"type": "string"},
                "property_type": {"type": "string"},
                "size_marla": {"type": "number"},
                "max_price": {"type": "integer"},
                "min_price": {"type": "integer"},
                "bedrooms": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["purpose"],
        },
    },
    {
        "type": "function",
        "name": "get_property_details",
        "description": "Fetch one property by id from the database.",
        "parameters": {
            "type": "object",
            "properties": {"property_id": {"type": "string", "format": "uuid"}},
            "required": ["property_id"],
        },
    },
    {
        "type": "function",
        "name": "create_or_update_lead",
        "description": "Persist extracted lead requirements. Does not invent inventory.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "format": "uuid"},
                "call_id": {"type": "string", "format": "uuid"},
                "purpose": {"type": "string", "enum": ["rent", "buy", "sell", "visit", "general"]},
                "location": {"type": "string"},
                "property_type": {"type": "string"},
                "size": {"type": "string"},
                "budget_min": {"type": "integer"},
                "budget_max": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["purpose"],
        },
    },
    {
        "type": "function",
        "name": "check_appointment_availability",
        "description": "Check if a visit slot is available before booking.",
        "parameters": {
            "type": "object",
            "properties": {"scheduled_at": {"type": "string", "format": "date-time"}},
            "required": ["scheduled_at"],
        },
    },
    {
        "type": "function",
        "name": "book_appointment",
        "description": "Book only after availability check. Speak success only if success=true.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "format": "uuid"},
                "property_id": {"type": "string", "format": "uuid"},
                "call_id": {"type": "string", "format": "uuid"},
                "scheduled_at": {"type": "string", "format": "date-time"},
                "notes": {"type": "string"},
            },
            "required": ["customer_id", "scheduled_at"],
        },
    },
    {
        "type": "function",
        "name": "transfer_to_human",
        "description": (
            "Transfer this caller to a human. Use when they ask for a person. "
            "Do not supply a phone number."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "department": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
    {
        "type": "function",
        "name": "request_human_handoff",
        "description": "Same as transfer_to_human. Do not supply a phone number.",
        "parameters": {
            "type": "object",
            "properties": {
                "call_id": {"type": "string", "format": "uuid"},
                "customer_id": {"type": "string", "format": "uuid"},
                "reason": {"type": "string"},
                "department": {"type": "string"},
                "context": {"type": "object"},
            },
            "required": ["reason"],
        },
    },
    {
        "type": "function",
        "name": "wait_for_user",
        "description": (
            "Stay silent. Use for silence, background noise, a nearby conversation, "
            "TV/radio, or speech not addressed to you. Does not hang up or change CRM data."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "register_opt_out",
        "description": "Mark the caller as opted out of further calls. Stop sales immediately.",
        "parameters": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "format": "uuid"},
                "phone": {"type": "string"},
                "reason": {"type": "string"},
            },
        },
    },
]


ALLOWED_TOOLS = {tool["name"] for tool in TOOL_DEFINITIONS}

_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "database_url",
    "redis_url",
    "sip_",
)


def _sanitize_tool_payload(value: Any) -> Any:
    """Strip secret-looking fields from tool args/results before persistence."""
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
                continue
            clean[key] = _sanitize_tool_payload(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_tool_payload(item) for item in value]
    return value


class ToolExecutor:
    """Backend-owned tool execution. The model proposes; this class decides."""

    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.properties = PropertyService(db, tenant_id)
        self.leads = LeadService(db, tenant_id)
        self.bookings = BookingService(db, tenant_id)

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        call_id: UUID | None = None,
        provider_tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        if tool_name not in ALLOWED_TOOLS:
            return await self._record(
                tool_name,
                arguments,
                ToolExecutionResult(
                    success=False,
                    tool_name=tool_name,
                    error=f"Tool '{tool_name}' is not allowed",
                    speakable_summary="I cannot perform that action.",
                ),
                call_id=call_id,
                provider_tool_call_id=provider_tool_call_id,
            )

        started_at = datetime.now(timezone.utc)
        handler_started = time.perf_counter()
        try:
            # Savepoint so a failed INSERT (for example a bad customer id) rolls
            # back only the tool write. The outer session stays usable for the
            # failure record; otherwise SQLAlchemy raises PendingRollbackError
            # and the voice monitor treats a live call as failed.
            async with self.db.begin_nested():
                result = await self._dispatch(tool_name, arguments, call_id=call_id)
        except Exception as exc:  # noqa: BLE001 - convert to tool failure for the model
            result = ToolExecutionResult(
                success=False,
                tool_name=tool_name,
                error=str(exc)[:500],
                speakable_summary="There was a problem completing that request.",
            )

        handler_ms = round((time.perf_counter() - handler_started) * 1000)
        if handler_ms >= 1000:
            logger.warning(
                "SLOW_TOOL tool=%s tool_execution_ms=%s band_ms=1000",
                tool_name,
                handler_ms,
            )
        elif handler_ms >= 500:
            logger.warning(
                "SLOW_TOOL tool=%s tool_execution_ms=%s band_ms=500",
                tool_name,
                handler_ms,
            )
        elif handler_ms >= 300:
            logger.info(
                "SLOW_TOOL tool=%s tool_execution_ms=%s band_ms=300",
                tool_name,
                handler_ms,
            )
        else:
            logger.info("TOOL_EXECUTION tool=%s tool_execution_ms=%s", tool_name, handler_ms)

        return await self._record(
            tool_name,
            arguments,
            result,
            call_id=call_id,
            provider_tool_call_id=provider_tool_call_id,
            started_at=started_at,
        )

    async def _dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        call_id: UUID | None,
    ) -> ToolExecutionResult:
        if tool_name == "search_properties":
            return await self._search_properties(arguments)
        if tool_name == "get_property_details":
            return await self._get_property_details(arguments)
        if tool_name == "create_or_update_lead":
            return await self._create_lead(arguments)
        if tool_name == "check_appointment_availability":
            return await self._check_availability(arguments)
        if tool_name == "book_appointment":
            return await self._book_appointment(arguments)
        if tool_name in {"transfer_to_human", "request_human_handoff"}:
            return await self._transfer_to_human(arguments, call_id=call_id)
        if tool_name == "register_opt_out":
            return await self._register_opt_out(arguments, call_id=call_id)
        if tool_name == "wait_for_user":
            return ToolExecutionResult(
                success=True,
                tool_name=tool_name,
                data={"silent": True, "no_response": True},
                speakable_summary="",
            )
        return ToolExecutionResult(
            success=False,
            tool_name=tool_name,
            error="Unhandled tool",
            speakable_summary="I cannot complete that right now.",
        )

    async def _record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolExecutionResult,
        *,
        call_id: UUID | None,
        provider_tool_call_id: str | None = None,
        started_at: datetime | None = None,
    ) -> ToolExecutionResult:
        safe_args = _sanitize_tool_payload(arguments)
        safe_result = _sanitize_tool_payload(result.model_dump())
        completed_at = datetime.now(timezone.utc)
        row = ToolCall(
            tenant_id=self.tenant_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments=safe_args,
            result=safe_result,
            success=result.success,
            error=result.error,
            provider_tool_call_id=provider_tool_call_id,
            started_at=started_at or completed_at,
            completed_at=completed_at,
        )
        self.db.add(row)
        await self.db.flush()
        return result

    async def _search_properties(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        purpose = Purpose(arguments.get("purpose", "general"))
        filters = PropertySearch(
            purpose=purpose,
            city=arguments.get("city"),
            area=arguments.get("area"),
            property_type=arguments.get("property_type"),
            size_marla=arguments.get("size_marla"),
            max_price=arguments.get("max_price") or arguments.get("max_rent"),
            min_price=arguments.get("min_price"),
            bedrooms=arguments.get("bedrooms"),
            limit=arguments.get("limit", 5),
        )
        properties = await self.properties.search(filters)
        data = [
            {
                "id": str(p.id),
                "title": p.title,
                "area": p.area,
                "city": p.city,
                "size": p.size,
                "size_marla": p.size_marla,
                "price": p.price,
                "currency": p.currency,
                "property_type": p.property_type,
                "status": p.status.value,
            }
            for p in properties
        ]
        if not data:
            summary = "No matching properties were found in inventory for those filters."
        else:
            summary = f"Found {len(data)} matching properties from live inventory."
        return ToolExecutionResult(
            success=True,
            tool_name="search_properties",
            data={"properties": data, "count": len(data)},
            speakable_summary=summary,
        )

    async def _get_property_details(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        property_id = UUID(arguments["property_id"])
        prop = await self.properties.get(property_id)
        if prop is None:
            return ToolExecutionResult(
                success=False,
                tool_name="get_property_details",
                error="Property not found",
                speakable_summary="That property was not found in inventory.",
            )
        data = {
            "id": str(prop.id),
            "title": prop.title,
            "area": prop.area,
            "city": prop.city,
            "size": prop.size,
            "price": prop.price,
            "currency": prop.currency,
            "status": prop.status.value,
            "description": prop.description,
            "bedrooms": prop.bedrooms,
            "bathrooms": prop.bathrooms,
        }
        return ToolExecutionResult(
            success=True,
            tool_name="get_property_details",
            data=data,
            speakable_summary=f"{prop.title} in {prop.area} is currently {prop.status.value}.",
        )

    async def _customer_on_file(self, customer_id: UUID) -> UUID | None:
        found = await self.db.execute(
            select(Customer.id).where(
                Customer.id == customer_id,
                Customer.tenant_id == self.tenant_id,
            )
        )
        return found.scalar_one_or_none()

    async def _customer_id_for_lead(self, arguments: dict[str, Any]) -> UUID | None:
        """Use a real customer. The model often invents a customer_id UUID."""
        raw = arguments.get("customer_id")
        if raw:
            try:
                candidate = UUID(str(raw))
            except (TypeError, ValueError):
                candidate = None
            if candidate is not None:
                existing = await self._customer_on_file(candidate)
                if existing is not None:
                    return existing

        call_raw = arguments.get("call_id")
        if not call_raw:
            return None
        try:
            call_id = UUID(str(call_raw))
        except (TypeError, ValueError):
            return None
        found_call = await self.db.execute(
            select(Call.customer_id).where(
                Call.id == call_id,
                Call.tenant_id == self.tenant_id,
            )
        )
        return found_call.scalar_one_or_none()

    async def _create_lead(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        lead = await self.leads.upsert_from_requirements(
            customer_id=await self._customer_id_for_lead(arguments),
            call_id=UUID(arguments["call_id"]) if arguments.get("call_id") else None,
            purpose=Purpose(arguments.get("purpose", "general")),
            location=arguments.get("location"),
            property_type=arguments.get("property_type"),
            size=arguments.get("size"),
            budget_min=arguments.get("budget_min"),
            budget_max=arguments.get("budget_max") or arguments.get("budget"),
            notes=arguments.get("notes"),
        )
        return ToolExecutionResult(
            success=True,
            tool_name="create_or_update_lead",
            data={"lead_id": str(lead.id), "status": lead.status.value},
            speakable_summary="I have saved your requirements.",
        )

    async def _check_availability(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        scheduled_at = datetime.fromisoformat(arguments["scheduled_at"].replace("Z", "+00:00"))
        data = await self.bookings.check_availability(scheduled_at)
        summary = (
            "That time slot is available."
            if data["available"]
            else "That time slot is not available. Please choose another time."
        )
        return ToolExecutionResult(
            success=True,
            tool_name="check_appointment_availability",
            data=data,
            speakable_summary=summary,
        )

    async def _book_appointment(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        payload = AppointmentCreate(
            customer_id=UUID(arguments["customer_id"]),
            property_id=UUID(arguments["property_id"]) if arguments.get("property_id") else None,
            call_id=UUID(arguments["call_id"]) if arguments.get("call_id") else None,
            scheduled_at=datetime.fromisoformat(arguments["scheduled_at"].replace("Z", "+00:00")),
            notes=arguments.get("notes"),
        )
        appointment = await self.bookings.book(payload)
        return ToolExecutionResult(
            success=True,
            tool_name="book_appointment",
            data={
                "success": True,
                "booking_id": str(appointment.id),
                "booking_code": appointment.booking_code,
                "scheduled_at": appointment.scheduled_at.isoformat(),
                "status": appointment.status.value,
            },
            speakable_summary=(
                f"Your appointment has been confirmed. Booking code {appointment.booking_code}."
            ),
        )

    async def _transfer_to_human(
        self,
        arguments: dict[str, Any],
        *,
        call_id: UUID | None,
    ) -> ToolExecutionResult:
        # Model must never choose the destination number.
        safe_args = strip_model_destination_args(arguments)
        reason = str(safe_args.get("reason") or "customer_requested_human").strip()
        department = safe_args.get("department")
        context = safe_args.get("context") if isinstance(safe_args.get("context"), dict) else {}

        resolved_call_id = call_id
        if resolved_call_id is None and safe_args.get("call_id"):
            try:
                resolved_call_id = UUID(str(safe_args["call_id"]))
            except (TypeError, ValueError):
                resolved_call_id = None

        if resolved_call_id is None:
            return ToolExecutionResult(
                success=False,
                tool_name="transfer_to_human",
                error="call_id_required",
                speakable_summary="I could not start the transfer right now.",
            )

        result = await prepare_handoff(
            self.db,
            tenant_id=self.tenant_id,
            call_id=resolved_call_id,
            reason=reason[:500],
            department=str(department)[:64] if department else None,
            context=context,
        )
        tool_name = "transfer_to_human"
        if not result.get("ok"):
            return ToolExecutionResult(
                success=False,
                tool_name=tool_name,
                error=str(result.get("error") or "handoff_failed"),
                data={
                    "initiate_redirect": False,
                    "destination_masked": result.get("destination_masked"),
                },
                speakable_summary=str(
                    result.get("speakable_summary")
                    or "I'm sorry, a representative is not available right now."
                ),
            )

        return ToolExecutionResult(
            success=True,
            tool_name=tool_name,
            data={
                "initiate_redirect": True,
                "destination_masked": result.get("destination_masked"),
                "call_sid_suffix": str(result.get("call_sid") or "")[-4:] or None,
                "status": "requested",
                # Confirm we ignored any model-supplied number.
                "model_destination_ignored": True,
            },
            speakable_summary=str(
                result.get("speakable_summary")
                or "Sure, I'll connect you to a representative."
            ),
        )

    async def _register_opt_out(
        self,
        arguments: dict[str, Any],
        *,
        call_id: UUID | None,
    ) -> ToolExecutionResult:
        customers = CustomerService(self.db, self.tenant_id)
        customer = None
        if arguments.get("customer_id"):
            result = await self.db.execute(
                select(Customer).where(
                    Customer.id == UUID(arguments["customer_id"]),
                    Customer.tenant_id == self.tenant_id,
                )
            )
            customer = result.scalar_one_or_none()
        elif arguments.get("phone"):
            customer = await customers.get_or_create_by_phone(str(arguments["phone"]))
        elif call_id is not None:
            call_result = await self.db.execute(
                select(Call).where(Call.id == call_id, Call.tenant_id == self.tenant_id)
            )
            call = call_result.scalar_one_or_none()
            if call and call.customer_id:
                cust_result = await self.db.execute(
                    select(Customer).where(
                        Customer.id == call.customer_id,
                        Customer.tenant_id == self.tenant_id,
                    )
                )
                customer = cust_result.scalar_one_or_none()

        if customer is None:
            return ToolExecutionResult(
                success=False,
                tool_name="register_opt_out",
                error="customer_not_found",
                speakable_summary="I could not locate your record to complete the opt-out.",
            )

        meta = dict(customer.metadata_json or {})
        meta["opted_out"] = True
        meta["opted_out_at"] = datetime.now(timezone.utc).isoformat()
        if arguments.get("reason"):
            meta["opt_out_reason"] = str(arguments["reason"])[:500]
        customer.metadata_json = meta
        await self.db.flush()
        return ToolExecutionResult(
            success=True,
            tool_name="register_opt_out",
            data={"customer_id": str(customer.id), "opted_out": True},
            speakable_summary="Understood. I have marked your request to not receive further calls.",
        )
