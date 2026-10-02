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


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def get_app_settings():
    return get_settings()
