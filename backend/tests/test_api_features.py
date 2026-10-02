"""Tests for newly added dashboard APIs and config safety."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.config import Settings, validate_required_settings
from app.models import CallStatus, CampaignStatus, HandoffStatus
from app.schemas import (
    AgentConfigOut,
    AgentConfigUpdate,
    AnalyticsOut,
    CallDetailOut,
    DashboardStatsOut,
    HangupResponse,
    HandoffUpdate,
)
from app.services import AgentConfigService, CallService, CampaignService, HandoffService


def test_validate_settings_allows_development_without_voice():
    settings = Settings(
        app_env="development",
        openai_api_key="",
        sip_trunk_id="",
        twilio_account_sid="",
    )
    validate_required_settings(settings)


def test_validate_settings_fails_on_missing_voice_env_names_only():
    settings = Settings(
        app_env="development",
        openai_sip_project_id="proj",
        sip_trunk_id="",
        openai_api_key="",
        openai_realtime_model="",
        openai_webhook_secret="",
        twilio_api_key_sid="",
        twilio_api_key_secret="",
        sip_provider_api_key="",
    )
    with pytest.raises(RuntimeError) as exc:
        validate_required_settings(settings)
    message = str(exc.value)
    assert "Missing required environment variable:" in message
    assert "OPENAI_API_KEY" in message
    assert "OPENAI_WEBHOOK_SECRET" in message
    assert "SIP_TRUNK_ID" in message
    assert "sk-" not in message


def test_validate_settings_allows_twilio_keys_without_account_sid():
    """Inbound SIP can start without Account SID; hangup needs it later."""
    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        database_url_sync="postgresql://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        jwt_secret="prod-jwt-secret-value",
        secret_key="prod-secret-key-value",
        openai_api_key="sk-test",
        openai_realtime_model="gpt-realtime",
        openai_sip_project_id="proj_x",
        openai_webhook_secret="whsec_test",
        sip_trunk_id="TK123",
        twilio_api_key_sid="SK123",
        twilio_api_key_secret="secret",
        twilio_account_sid="",
    )
    validate_required_settings(settings)


def test_agent_config_out_has_no_secret_fields():
    payload = AgentConfigOut(
        agent_name="Agent",
        business_name="Biz",
        greeting="Hi",
        supported_languages=["English"],
        voice="alloy",
        max_call_duration=10,
        max_clarification_attempts=2,
        human_handoff_enabled=True,
        silence_timeout=8,
        system_instructions="Be helpful",
    )
    dumped = payload.model_dump()
    forbidden = {
        "openai_api_key",
        "twilio_api_key_secret",
        "sip_password",
        "jwt_secret",
        "database_url",
        "redis_url",
        "secret_key",
    }
    assert forbidden.isdisjoint(dumped.keys())


@pytest.mark.asyncio
async def test_hangup_does_not_complete_when_provider_fails():
    call_id = uuid4()
    tenant_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        tenant_id=tenant_id,
        status=CallStatus.ACTIVE,
        provider_call_id="CA123",
        started_at=datetime.now(timezone.utc),
    )
    db = AsyncMock()
    service = CallService(db, tenant_id)
    service.get = AsyncMock(return_value=call)
    service.add_event = AsyncMock()
    service.set_status = AsyncMock()

    with patch("app.voice.sip.SipClient") as sip_cls:
        sip_cls.return_value.hangup = AsyncMock(
            return_value={"ok": False, "mode": "twilio", "error": "twilio_hangup_rejected", "message": "fail"}
        )
        result = await service.hangup(call_id)

    assert isinstance(result, HangupResponse)
    assert result.success is False
    service.set_status.assert_not_called()


@pytest.mark.asyncio
async def test_hangup_marks_completed_on_provider_success():
    call_id = uuid4()
    tenant_id = uuid4()
    call = SimpleNamespace(
        id=call_id,
        tenant_id=tenant_id,
        status=CallStatus.ACTIVE,
        provider_call_id="CA123",
        started_at=datetime.now(timezone.utc),
    )
    db = AsyncMock()
    service = CallService(db, tenant_id)
    service.get = AsyncMock(return_value=call)
    service.add_event = AsyncMock()
    service.set_status = AsyncMock(return_value=call)

    with patch("app.voice.sip.SipClient") as sip_cls:
        sip_cls.return_value.hangup = AsyncMock(
            return_value={"ok": True, "mode": "stub", "message": "local"}
        )
        result = await service.hangup(call_id)

    assert result.success is True
    service.set_status.assert_awaited()


@pytest.mark.asyncio
async def test_handoff_accept_and_resolve():
    handoff_id = uuid4()
    tenant_id = uuid4()
    handoff = SimpleNamespace(
        id=handoff_id,
        tenant_id=tenant_id,
        call_id=uuid4(),
        customer_id=None,
        assigned_user_id=None,
        reason="needs human",
        status=HandoffStatus.REQUESTED,
        context={},
        created_at=datetime.now(timezone.utc),
    )
    db = AsyncMock()
    service = HandoffService(db, tenant_id)
    service.get = AsyncMock(return_value=handoff)

    accepted = await service.update(
        handoff_id,
        HandoffUpdate(status=HandoffStatus.ACCEPTED, assigned_to=uuid4()),
    )
    assert accepted is not None
    assert accepted.status == HandoffStatus.ACCEPTED

    handoff.status = HandoffStatus.ACCEPTED
    resolved = await service.update(
        handoff_id,
        HandoffUpdate(status=HandoffStatus.COMPLETED, resolution_notes="Handled"),
    )
    assert resolved is not None
    assert resolved.status == HandoffStatus.COMPLETED
    assert resolved.resolution_notes == "Handled"


@pytest.mark.asyncio
async def test_campaign_pause_resume_and_concurrency_gate():
    campaign_id = uuid4()
    tenant_id = uuid4()
    campaign = SimpleNamespace(
        id=campaign_id,
        tenant_id=tenant_id,
        status=CampaignStatus.RUNNING,
    )
    db = AsyncMock()
    service = CampaignService(db, tenant_id)
    service.get = AsyncMock(return_value=campaign)

    paused = await service.pause(campaign_id)
    assert paused is not None
    assert paused.status == CampaignStatus.PAUSED

    with patch.object(service, "_concurrency_ok", AsyncMock(return_value=(False, "limit"))):
        with pytest.raises(ValueError, match="limit"):
            await service.resume(campaign_id)

    with patch.object(service, "_concurrency_ok", AsyncMock(return_value=(True, "ok"))):
        resumed = await service.resume(campaign_id)
    assert resumed is not None
    assert resumed.status == CampaignStatus.RUNNING


@pytest.mark.asyncio
async def test_agent_config_strips_unsafe_keys():
    tenant_id = uuid4()
    tenant = SimpleNamespace(
        id=tenant_id,
        name="Synas Labs",
        settings={
            "agent_config": {
                "agent_name": "Aisha",
                "openai_api_key": "sk-should-never-leak",
                "twilio_api_key_secret": "secret",
            }
        },
    )
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=MagicMock(return_value=tenant))
    )
    service = AgentConfigService(db, tenant_id)
    config = await service.get()
    assert config.agent_name == "Aisha"
    dumped = config.model_dump()
    assert "openai_api_key" not in dumped
    assert "sk-should-never-leak" not in str(dumped)


def test_analytics_empty_defaults():
    empty = AnalyticsOut()
    assert empty.calls_per_day == []
    assert empty.language_distribution == []
    assert empty.status_distribution == []
    assert empty.cost_metrics is None


def test_dashboard_stats_schema():
    stats = DashboardStatsOut(
        active_calls=0,
        queued_calls=0,
        calls_today=0,
        completed_calls=0,
        failed_calls=0,
        leads_today=0,
        open_handoffs=0,
        appointments_today=0,
    )
    assert stats.active_calls == 0


def test_call_detail_schema_allows_empty_transcript():
    detail = CallDetailOut(
        id=uuid4(),
        tenant_id=uuid4(),
        customer_id=None,
        direction="inbound",
        status=CallStatus.COMPLETED,
        from_number="+1000",
        to_number="+2000",
        provider_call_id=None,
        openai_session_id=None,
        intent=None,
        started_at=None,
        ended_at=None,
        duration_seconds=None,
        failure_reason=None,
        created_at=datetime.now(timezone.utc),
        messages=[],
        tool_executions=[],
    )
    assert detail.messages == []
    assert detail.tool_executions == []


def test_agent_config_update_only_safe_fields():
    payload = AgentConfigUpdate(agent_name="New", voice="verse")
    assert set(payload.model_dump(exclude_unset=True).keys()) <= {
        "agent_name",
        "business_name",
        "greeting",
        "supported_languages",
        "voice",
        "max_call_duration",
        "max_clarification_attempts",
        "human_handoff_enabled",
        "silence_timeout",
        "system_instructions",
    }
