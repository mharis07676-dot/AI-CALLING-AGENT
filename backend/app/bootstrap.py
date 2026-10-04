from sqlalchemy import text

from app.config import get_settings
from app.db.session import engine
from app.db.base import Base

# Import models so metadata is registered
from app.models import (  # noqa: F401
    Tenant,
    User,
    Customer,
    Lead,
    Property,
    Appointment,
    Call,
    CallEvent,
    Conversation,
    Message,
    ToolCall,
    Handoff,
    AgentConfig,
    UsageRecord,
    AuditLog,
    Campaign,
    IdempotencyKey,
)


# create_all does not add columns to tables that already exist. These statements
# match the SQL migrations and are safe to run on every startup.
_SCHEMA_PATCHES = (
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS answered_at TIMESTAMPTZ",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_requested BOOLEAN DEFAULT FALSE",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_requested_at TIMESTAMPTZ",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_status VARCHAR(32)",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_reason TEXT",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_connected_at TIMESTAMPTZ",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS handoff_completed_at TIMESTAMPTZ",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_status VARCHAR(32)",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_format VARCHAR(16)",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_duration_seconds INTEGER",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_size_bytes INTEGER",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_storage_key VARCHAR(512)",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_provider VARCHAR(32)",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS recording_provider_sid VARCHAR(64)",
    "ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS provider_tool_call_id VARCHAR(128)",
    "ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
    "ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS ix_calls_handoff_status ON calls (handoff_status)",
    "CREATE INDEX IF NOT EXISTS ix_calls_handoff_requested ON calls (handoff_requested)",
    "CREATE INDEX IF NOT EXISTS ix_calls_recording_status ON calls (recording_status)",
    "CREATE INDEX IF NOT EXISTS ix_calls_recording_provider_sid ON calls (recording_provider_sid)",
    "CREATE INDEX IF NOT EXISTS ix_calls_recording_storage_key ON calls (recording_storage_key)",
    "CREATE INDEX IF NOT EXISTS ix_tool_calls_provider_tool_call_id ON tool_calls (provider_tool_call_id)",
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ensure_schema_patches() -> None:
    """Add columns introduced after the production tables were first created."""
    async with engine.begin() as conn:
        for statement in _SCHEMA_PATCHES:
            await conn.execute(text(statement))


def get_app_settings():
    return get_settings()
