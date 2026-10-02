"""Input/output validation helpers for AI tool payloads."""

from __future__ import annotations

from typing import Any

from app.ai.guardrails import validate_tool_arguments


def sanitize_tool_payload(tool_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors = validate_tool_arguments(tool_name, arguments)
    cleaned = {k: v for k, v in arguments.items() if v is not None}
    # Strip accidental free-form SQL-ish keys
    cleaned.pop("sql", None)
    cleaned.pop("raw_query", None)
    return cleaned, errors
