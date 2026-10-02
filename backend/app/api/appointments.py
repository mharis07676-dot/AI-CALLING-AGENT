from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, get_current_auth
from app.db.session import get_db
from app.schemas import AppointmentCreate, AppointmentOut
from app.services import BookingService

router = APIRouter(prefix="/appointments", tags=["appointments"])


@router.get("/", response_model=list[AppointmentOut])
async def list_appointments(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list:
    return await BookingService(db, auth.tenant_id).list()


@router.post("/check-availability")
async def check_availability(
    payload: AppointmentCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    return await BookingService(db, auth.tenant_id).check_availability(payload.scheduled_at)


@router.post("/", response_model=AppointmentOut, status_code=201)
async def book_appointment(
    payload: AppointmentCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AppointmentOut:
    try:
        return await BookingService(db, auth.tenant_id).book(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
