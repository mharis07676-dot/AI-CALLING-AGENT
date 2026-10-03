import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.bootstrap import init_db
from app.config import get_settings, validate_required_settings
from app.db.session import AsyncSessionLocal
from app.services import repair_answered_calls_marked_failed

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    validate_required_settings(settings)
    await init_db()
    async with AsyncSessionLocal() as db:
        repaired = await repair_answered_calls_marked_failed(db)
        await db.commit()
    logger.warning("Marked %s answered calls completed", repaired)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/")
    async def root() -> dict:
        return {
            "service": settings.app_name,
            "status": "ok",
            "docs": "/docs",
            "health": "/health",
            "api": settings.api_prefix,
            "note": "This is the API. The Agent Ops UI is the separate dashboard service.",
        }

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "service": settings.app_name,
            "env": settings.app_env,
            "max_concurrent_calls": settings.max_concurrent_calls,
            "voice_enabled": settings.voice_enabled,
            "twilio_hangup_configured": settings.twilio_hangup_configured,
            "openai_realtime_model": settings.openai_realtime_model,
        }

    return app


app = create_app()
