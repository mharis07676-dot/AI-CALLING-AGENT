from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Purpose, ToolCall
from app.schemas import (
    AppointmentCreate,
    HandoffCreate,
    PropertySearch,
    ToolExecutionResult,
)
from app.services import BookingService, HandoffService, LeadService, PropertyService


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
        "name": "request_human_handoff",
        "description": "Escalate to a human sales agent when confidence is low or caller asks.",
        "parameters": {
            "type": "object",
            "properties": {
                "call_id": {"type": "string", "format": "uuid"},
                "customer_id": {"type": "string", "format": "uuid"},
                "reason": {"type": "string"},
                "context": {"type": "object"},
            },
            "required": ["call_id", "reason"],
        },
    },
]


ALLOWED_TOOLS = {tool["name"] for tool in TOOL_DEFINITIONS}


class ToolExecutor:
    """Backend-owned tool execution. The model proposes; this class decides."""

    def __init__(self, db: AsyncSession, tenant_id: UUID):
        self.db = db
        self.tenant_id = tenant_id
        self.properties = PropertyService(db, tenant_id)
        self.leads = LeadService(db, tenant_id)
        self.bookings = BookingService(db, tenant_id)
        self.handoffs = HandoffService(db, tenant_id)

    async def execute(self, tool_name: str, arguments: dict[str, Any], *, call_id: UUID | None = None) -> ToolExecutionResult:
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
            )

        try:
            if tool_name == "search_properties":
                result = await self._search_properties(arguments)
            elif tool_name == "get_property_details":
                result = await self._get_property_details(arguments)
            elif tool_name == "create_or_update_lead":
                result = await self._create_lead(arguments)
            elif tool_name == "check_appointment_availability":
                result = await self._check_availability(arguments)
            elif tool_name == "book_appointment":
                result = await self._book_appointment(arguments)
            elif tool_name == "request_human_handoff":
                result = await self._request_handoff(arguments)
            else:
                # Exhaustive for allowed tools; keep fail-closed.
                result = ToolExecutionResult(
                    success=False,
                    tool_name=tool_name,
                    error="Unhandled tool",
                    speakable_summary="I cannot complete that right now.",
                )
        except Exception as exc:  # noqa: BLE001 - convert to tool failure for the model
            result = ToolExecutionResult(
                success=False,
                tool_name=tool_name,
                error=str(exc),
                speakable_summary="There was a problem completing that request.",
            )

        return await self._record(tool_name, arguments, result, call_id=call_id)

    async def _record(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolExecutionResult,
        *,
        call_id: UUID | None,
    ) -> ToolExecutionResult:
        row = ToolCall(
            tenant_id=self.tenant_id,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            result=result.model_dump(),
            success=result.success,
            error=result.error,
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

    async def _create_lead(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        lead = await self.leads.upsert_from_requirements(
            customer_id=UUID(arguments["customer_id"]) if arguments.get("customer_id") else None,
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

    async def _request_handoff(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        payload = HandoffCreate(
            call_id=UUID(arguments["call_id"]),
            customer_id=UUID(arguments["customer_id"]) if arguments.get("customer_id") else None,
            reason=arguments["reason"],
            context=arguments.get("context") or {},
        )
        handoff = await self.handoffs.request(payload)
        return ToolExecutionResult(
            success=True,
            tool_name="request_human_handoff",
            data={"handoff_id": str(handoff.id), "status": handoff.status.value},
            speakable_summary="I am connecting you with a human agent now.",
        )
