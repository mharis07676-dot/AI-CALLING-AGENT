from fastapi import APIRouter

from app.api import agent, analytics, appointments, auth, calls, campaigns, dashboard, handoffs, leads, properties, webhooks

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(calls.router)
api_router.include_router(handoffs.router)
api_router.include_router(leads.router)
api_router.include_router(properties.router)
api_router.include_router(appointments.router)
api_router.include_router(dashboard.router)
api_router.include_router(agent.router)
api_router.include_router(campaigns.router)
api_router.include_router(analytics.router)
api_router.include_router(webhooks.router)
