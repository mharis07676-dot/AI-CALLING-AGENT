import pytest
from app.ai.guardrails import should_handoff_after_clarifications, validate_tool_arguments
from app.services.booking_codes import generate_booking_code


def test_booking_code_format():
    code = generate_booking_code()
    assert code.startswith("BK")
    assert len(code) == 8


def test_handoff_after_two_clarifications():
    assert should_handoff_after_clarifications(1) is False
    assert should_handoff_after_clarifications(2) is True


def test_book_appointment_requires_fields():
    errors = validate_tool_arguments("book_appointment", {"customer_id": "x"})
    assert "book_appointment requires scheduled_at" in errors
