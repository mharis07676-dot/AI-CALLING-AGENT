from fastapi import APIRouter

from app.api import appointments, auth, calls, leads, properties, webhooks

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(calls.router)
api_router.include_router(leads.router)
api_router.include_router(properties.router)
api_router.include_router(appointments.router)
api_router.include_router(webhooks.router)
