from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.tools import ToolExecutor
from app.auth import AuthContext, auth_from_token, bearer_scheme, get_current_auth
from app.db.session import get_db
from app.schemas import (
    CallCreate,
    CallDetailOut,
    CallOut,
    HandoffCreate,
    HandoffOut,
    HangupResponse,
    ToolExecutionResult,
)
from app.services import CallService, HandoffService
from app.voice.call_manager import CallManager
from app.voice.realtime import create_realtime_session_stub
from app.voice.recording import recordings_root, resolve_recording_path
from app.voice.storage import build_recording_storage

router = APIRouter(prefix="/calls", tags=["calls"])


def _recording_media_type(fmt: str | None) -> str:
    normalized = (fmt or "mp3").lower()
    if normalized == "wav":
        return "audio/wav"
    return "audio/mpeg"


def _safe_download_filename(call_id: UUID, fmt: str | None, started_at: datetime | None) -> str:
    stamp = (started_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    ext = (fmt or "mp3").lower()
    return f"call_{str(call_id)[:8]}_{stamp}.{ext}"


async def _media_auth(
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    access_token: Annotated[str | None, Query()] = None,
) -> AuthContext:
    """Bearer header or access_token query (HTML5 <audio> cannot set Authorization)."""
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    elif access_token:
        token = access_token.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await auth_from_token(token, db)


async def _load_ready_recording(auth: AuthContext, db: AsyncSession, call_id: UUID):
    call = await CallService(db, auth.tenant_id).get(call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    key = getattr(call, "recording_storage_key", None)
    status = getattr(call, "recording_status", None)
    if status != "ready" or not key:
        raise HTTPException(status_code=404, detail="Recording not available")
    return call, str(key)


def _file_or_bytes_response(
    *,
    call_id: UUID,
    call,
    key: str,
    disposition: str,
) -> FileResponse | Response:
    fmt = (getattr(call, "recording_format", None) or "mp3").lower()
    media_type = _recording_media_type(fmt)
    filename = _safe_download_filename(call_id, fmt, call.started_at or call.created_at)
    path = resolve_recording_path(key)
    if path is not None and path.is_file():
        try:
            path.resolve().relative_to(recordings_root().resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Recording not available") from exc
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename,
            content_disposition_type=disposition,
        )
    data = build_recording_storage().get(key)
    if not data:
        raise HTTPException(status_code=404, detail="Recording file missing")
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(data)),
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        },
    )


@router.get("/live", response_model=list[CallOut])
async def live_calls(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CallOut]:
    return await CallService(db, auth.tenant_id).list_live()


@router.get("/", response_model=list[CallOut])
async def list_calls(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[CallOut]:
    return await CallService(db, auth.tenant_id).list_recent()


@router.post("/inbound", response_model=CallOut)
async def admit_inbound_call(
    payload: CallCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CallOut:
    manager = CallManager(db, auth.tenant_id)
    decision, call = await manager.admit_inbound(
        from_number=payload.from_number,
        to_number=payload.to_number,
        provider_call_id=payload.provider_call_id,
    )
    if call is None:
        raise HTTPException(status_code=503, detail=decision.reason)
    if not decision.accepted:
        raise HTTPException(
            status_code=429,
            detail={
                "reason": decision.reason,
                "active_calls": decision.active_calls,
                "limit": decision.limit,
                "call_id": str(call.id),
            },
        )
    await create_realtime_session_stub(tenant_id=auth.tenant_id, call_id=call.id)
    return CallService(db, auth.tenant_id)._to_out(call)


@router.post("/handoffs", response_model=HandoffOut)
async def create_handoff(
    payload: HandoffCreate,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HandoffOut:
    service = HandoffService(db, auth.tenant_id)
    try:
        handoff = await service.request(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return service._to_out(handoff)


@router.get("/handoffs/open", response_model=list[HandoffOut])
async def open_handoffs(
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[HandoffOut]:
    return await HandoffService(db, auth.tenant_id).list_open()


@router.get("/{call_id}", response_model=CallDetailOut)
async def get_call(
    call_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CallDetailOut:
    detail = await CallService(db, auth.tenant_id).get_detail(call_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return detail


@router.get("/{call_id}/recording", response_model=None)
async def get_call_recording(
    call_id: UUID,
    auth: Annotated[AuthContext, Depends(_media_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FileResponse | Response:
    """Stream the stored call recording (authenticated). FileResponse supports HTTP Range seek."""
    call, key = await _load_ready_recording(auth, db, call_id)
    return _file_or_bytes_response(call_id=call_id, call=call, key=key, disposition="inline")


@router.get("/{call_id}/recording/download", response_model=None)
async def download_call_recording(
    call_id: UUID,
    auth: Annotated[AuthContext, Depends(_media_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> FileResponse | Response:
    """Authenticated download with Content-Disposition: attachment."""
    call, key = await _load_ready_recording(auth, db, call_id)
    return _file_or_bytes_response(call_id=call_id, call=call, key=key, disposition="attachment")


@router.post("/{call_id}/hangup", response_model=HangupResponse)
async def hangup_call(
    call_id: UUID,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HangupResponse:
    try:
        result = await CallService(db, auth.tenant_id).hangup(call_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Call not found") from exc
    if not result.success and result.error == "call_not_active":
        raise HTTPException(status_code=400, detail=result.message)
    return result


@router.post("/{call_id}/tools/{tool_name}", response_model=ToolExecutionResult)
async def execute_tool(
    call_id: UUID,
    tool_name: str,
    arguments: dict,
    auth: Annotated[AuthContext, Depends(get_current_auth)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ToolExecutionResult:
    executor = ToolExecutor(db, auth.tenant_id)
    return await executor.execute(tool_name, arguments, call_id=call_id)
