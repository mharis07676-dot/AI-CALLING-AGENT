from __future__ import annotations

from typing import Any


FORBIDDEN_CLAIM_PATTERNS = (
    "your appointment is booked",
    "appointment has been booked",
    "i have booked",
    "property is available",
    "yes it is available",
    "we have many houses",
)


def assert_no_unverified_business_claim(text: str, *, tool_confirmed: bool) -> list[str]:
    """Return warnings when the model may be inventing business facts."""
    lowered = text.lower()
    warnings: list[str] = []
    if tool_confirmed:
        return warnings
    for pattern in FORBIDDEN_CLAIM_PATTERNS:
        if pattern in lowered:
            warnings.append(f"Unverified business claim detected: '{pattern}'")
    return warnings


def validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if tool_name == "search_properties" and "purpose" not in arguments:
        errors.append("search_properties requires purpose")
    if tool_name == "book_appointment":
        for field in ("customer_id", "scheduled_at"):
            if field not in arguments:
                errors.append(f"book_appointment requires {field}")
    if tool_name in {"request_human_handoff", "transfer_to_human"}:
        if "reason" not in arguments:
            errors.append(f"{tool_name} requires reason")
        for banned in ("phone", "phone_number", "destination", "destination_number", "to_number"):
            if banned in arguments:
                errors.append(f"{tool_name} must not include {banned}")
    if "sql" in tool_name.lower() or "query" in arguments and isinstance(arguments.get("query"), str):
        if "select " in str(arguments.get("query", "")).lower():
            errors.append("Raw SQL is not permitted")
    return errors


def should_handoff_after_clarifications(failed_clarifications: int, max_attempts: int = 2) -> bool:
    return failed_clarifications >= max_attempts
