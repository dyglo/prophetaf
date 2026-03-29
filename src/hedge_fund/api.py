from __future__ import annotations

import json
import os
import re
from pathlib import Path
from queue import Empty, Queue
from threading import Lock
from threading import Thread
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from hedge_fund.chat.command import ChatCommandRunner
from hedge_fund.chat.ai import ChatLanguageService
from hedge_fund.chat.agent_runtime import AgentEventSink
from hedge_fund.chat.models import ChatResponse, ChatTurn
from hedge_fund.chat.session_store import DatabaseSessionStore, SessionNotFoundError
from hedge_fund.cli.bootstrap import ApplicationContext
from hedge_fund.services.prophet_md_generator import generate_prophet_md
from hedge_fund.services.calendar_service import CalendarService
from hedge_fund.services.scan_service import RiskService, ScanService

app = FastAPI(title="Prophet API")


class ClientChatMessage(BaseModel):
    role: str
    content: str
    metadata: dict = Field(default_factory=dict)


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    model: str | None = None
    stream: bool = False
    history: list[ClientChatMessage] = Field(default_factory=list)
    messages: list[ClientChatMessage] = Field(default_factory=list)
    image_b64: str | None = None
    image_b64_2: str | None = None
    media_type: Literal["image/png", "image/jpeg", "image/webp"] | None = None
    media_type_2: Literal["image/png", "image/jpeg", "image/webp"] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_image_fields(cls, values):
        if not isinstance(values, dict):
            return values
        normalized = dict(values)
        for field_name in ("image_b64", "image_b64_2", "media_type", "media_type_2"):
            value = normalized.get(field_name)
            if isinstance(value, str):
                stripped = value.strip()
                normalized[field_name] = stripped or None
        return normalized

    @model_validator(mode="after")
    def validate_image_fields(self):
        if self.image_b64 and not self.media_type:
            raise ValueError("media_type is required when image_b64 is provided")
        if self.image_b64_2 and not self.media_type_2:
            raise ValueError("media_type_2 is required when image_b64_2 is provided")
        return self

    def image_attachments(self) -> list[dict[str, str]]:
        attachments: list[dict[str, str]] = []
        if self.image_b64 and self.media_type:
            attachments.append({"base64": self.image_b64, "media_type": self.media_type})
        if self.image_b64_2 and self.media_type_2:
            attachments.append({"base64": self.image_b64_2, "media_type": self.media_type_2})
        return attachments


class ScanRequest(BaseModel):
    pair: str | None = None


class RiskRequest(BaseModel):
    pair: str
    risk: float
    sl: int


class MemoryRequest(BaseModel):
    content: str


class OnboardRequest(BaseModel):
    display_name: str = Field(min_length=2)
    experience_level: str
    watchlist: list[str] = Field(min_length=1)
    account_balance: float = Field(gt=0)
    risk_pct: float
    min_rr: str
    sessions: list[str] = Field(min_length=1)


class OnboardResponse(BaseModel):
    device_token: str
    display_name: str
    prophet_md_preview: str
    message: str


class ProfileResponse(BaseModel):
    device_token: str
    display_name: str
    experience_level: str
    watchlist: list[str]
    account_balance: float
    risk_pct: float
    min_rr: str
    sessions: list[str]
    created_at: str


class ProfileUpdateRequest(BaseModel):
    experience_level: str | None = None
    account_balance: float | None = None
    risk_pct: float | None = None
    watchlist: list[str] | None = None
    sessions: list[str] | None = None
    min_rr: str | None = None


_context: ApplicationContext | None = None
_context_lock = Lock()


def get_context() -> ApplicationContext:
    global _context
    if _context is None:
        with _context_lock:
            if _context is None:
                _context = ApplicationContext()
    return _context


def get_db_session(context: Annotated[ApplicationContext, Depends(get_context)]):
    with context.session_scope() as session:
        yield session


def get_device_token(device_token: Annotated[str | None, Header(alias="X-Device-Token")] = None) -> str | None:
    token = (device_token or "").strip()
    return token or None


def get_chat_session_store(context: Annotated[ApplicationContext, Depends(get_context)]) -> DatabaseSessionStore:
    language = ChatLanguageService(
        context.settings,
        context.env,
        context.logger,
    )
    return DatabaseSessionStore(
        context.session_factory,
        max_stored_sessions=context.settings.sessions.max_stored,
        summary_generator=language.summarize_session,
    )


def create_scan_service(context: ApplicationContext, session: Session) -> ScanService:
    return ScanService(
        context.settings,
        context.market_data,
        context.ai,
        context.create_repository(session),
        context.logger,
    )


def create_calendar_service(context: ApplicationContext) -> CalendarService:
    return CalendarService(context.calendar)


def _sync_request_history(state, request: ChatRequest) -> None:
    request_history = request.history or request.messages
    if not request_history:
        return
    turns = [ChatTurn(role=item.role, content=item.content, metadata=item.metadata) for item in request_history]
    if turns and turns[-1].role == "user" and turns[-1].content.strip() == request.message.strip():
        turns = turns[:-1]
    state.session.turns = turns


def _load_or_create_state(
    request: ChatRequest,
    context: ApplicationContext,
    session_store: DatabaseSessionStore,
) -> object:
    if request.session_id:
        try:
            return session_store.load(request.session_id)
        except SessionNotFoundError:
            pass
    return session_store.create(
        max_context_turns=context.settings.chat.max_context_turns,
        permission_mode="default",
        model_override=request.model,
        append_system_prompt=None,
    )


def _is_streamable_message_by_settings(settings, message: str) -> bool:
    if not getattr(settings, "streaming", None):
        return True
    if not settings.streaming.enabled:
        return False
    lowered = message.strip().lower()
    if not lowered or lowered.startswith("/"):
        return False
    if any(phrase in lowered for phrase in ("trade plan", "plan this trade", "generate a plan")):
        return True
    if "entry" in lowered and ("stop" in lowered or "stop loss" in lowered):
        return True
    blocked_terms = (
        "scan",
        "bias",
        "lot size",
        "risk",
        "calendar",
        "event",
        "news today",
        "rank",
        "best setup",
        "focus on",
        "compare",
    )
    return not any(term in lowered for term in blocked_terms)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _plan_steps(plan: str) -> list[str]:
    steps: list[str] = []
    for raw_line in str(plan or "").splitlines():
        line = re.sub(r"^\s*\d+[\.\)]\s*", "", raw_line).strip()
        if line:
            steps.append(line)
    return steps


class StreamingAgentEventSink(AgentEventSink):
    def __init__(self, queue: Queue[tuple[str, object]]) -> None:
        self.queue = queue

    def update_status(self, message: str) -> None:
        self.queue.put(("step", {"message": message}))

    def emit_plan(self, message: str) -> None:
        self.queue.put(("plan", {"steps": _plan_steps(message)}))

    def emit_reasoning(self, message: str) -> None:
        self.queue.put(("reasoning", {"message": message}))


def _profile_response(record) -> ProfileResponse:
    return ProfileResponse(
        device_token=record.device_token,
        display_name=record.display_name,
        experience_level=record.experience_level,
        watchlist=list(record.watchlist or []),
        account_balance=record.account_balance,
        risk_pct=record.risk_pct,
        min_rr=record.min_rr,
        sessions=list(record.sessions or []),
        created_at=record.created_at.isoformat(),
    )


def _memory_repository_for(context: ApplicationContext, session: Session, device_token: str | None):
    factory = getattr(context, "create_memory_repository", None)
    if not callable(factory):
        return None
    return factory(session, device_token=device_token)


def _update_profile_record(context: ApplicationContext, session: Session, device_token: str, request: ProfileUpdateRequest):
    repository = context.create_user_profile_repository(session)
    record = repository.get_by_device_token(device_token)
    if record is None:
        raise HTTPException(status_code=404, detail="Profile not found")

    changes = request.model_dump(exclude_none=True)
    if not changes:
        return record
    if "experience_level" in changes:
        preview_profile = type(
            "ProfilePreview",
            (),
            {
                "display_name": record.display_name,
                "experience_level": changes.get("experience_level", record.experience_level),
                "watchlist": changes.get("watchlist", list(record.watchlist or [])),
                "account_balance": changes.get("account_balance", record.account_balance),
                "risk_pct": changes.get("risk_pct", record.risk_pct),
                "min_rr": changes.get("min_rr", record.min_rr),
                "sessions": changes.get("sessions", list(record.sessions or [])),
            },
        )()
        changes["prophet_md"] = generate_prophet_md(preview_profile)
    updated = repository.update_by_device_token(device_token, **changes)
    if updated is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return updated


@app.get("/")
def read_root():
    return {"status": "ok", "app": "Prophet API"}


@app.post("/chat")
def chat(
    request: ChatRequest,
    context: Annotated[ApplicationContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db_session)],
    session_store: Annotated[DatabaseSessionStore, Depends(get_chat_session_store)],
    device_token: Annotated[str | None, Depends(get_device_token)] = None,
):
    state = _load_or_create_state(request, context, session_store)
    image_attachments = request.image_attachments()

    _sync_request_history(state, request)
    should_stream = request.stream and _is_streamable_message_by_settings(context.settings, request.message)
    if not should_stream:
        runner = ChatCommandRunner(
            context,
            cwd=Path(os.getcwd()),
            session_store=session_store,
            repository=context.create_repository(session),
        )
        service = runner.build_service(state.session.model_override, state.session.append_system_prompt, device_token=device_token)
        memory_repository = _memory_repository_for(context, session, device_token)
        if memory_repository is not None:
            service.memory_repository = memory_repository
        service.user_profile_repository = context.create_user_profile_repository(session) if device_token else None
        try:
            kwargs = {"image_attachments": image_attachments} if image_attachments else {}
            return service.process_message(state, request.message, **kwargs)
        finally:
            runner.close()

    def event_stream():
        queue: Queue[tuple[str, object]] = Queue()

        def worker() -> None:
            worker_runner = None
            try:
                with context.session_scope() as worker_session:
                    worker_runner = ChatCommandRunner(
                        context,
                        cwd=Path(os.getcwd()),
                        session_store=session_store,
                        repository=context.create_repository(worker_session),
                    )
                    worker_service = worker_runner.build_service(
                        state.session.model_override,
                        state.session.append_system_prompt,
                        device_token=device_token,
                    )
                    memory_repository = _memory_repository_for(context, worker_session, device_token)
                    if memory_repository is not None:
                        worker_service.memory_repository = memory_repository
                    worker_service.user_profile_repository = context.create_user_profile_repository(worker_session) if device_token else None
                    process_kwargs = {"image_attachments": image_attachments} if image_attachments else {}
                    response = worker_service.process_message(
                        state,
                        request.message,
                        event_sink=StreamingAgentEventSink(queue),
                        stream_handler=lambda chunk: queue.put(("chunk", chunk)),
                        **process_kwargs,
                    )
                queue.put(("response", response.model_dump(mode="json")))
            except Exception as exc:  # noqa: BLE001
                queue.put(("error", str(exc)))
            finally:
                if worker_runner is not None:
                    worker_runner.close()
                queue.put(("done", None))

        Thread(target=worker, daemon=True).start()
        while True:
            try:
                kind, payload = queue.get(timeout=0.1)
            except Empty:
                continue
            if kind == "chunk":
                yield _sse("message", {"delta": payload})
                continue
            if kind == "step":
                yield _sse("step", payload if isinstance(payload, dict) else {})
                continue
            if kind == "plan":
                yield _sse("plan", payload if isinstance(payload, dict) else {})
                continue
            if kind == "reasoning":
                yield _sse("reasoning", payload if isinstance(payload, dict) else {})
                continue
            if kind == "response":
                yield _sse("done", payload if isinstance(payload, dict) else {})
                continue
            if kind == "error":
                yield _sse("error", {"message": payload})
                continue
            if kind == "done":
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/scan")
def scan_endpoint(
    request: ScanRequest,
    context: Annotated[ApplicationContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db_session)],
):
    service = create_scan_service(context, session)
    pairs = [request.pair] if request.pair else context.settings.trading.pairs
    return service.scan(pairs)


@app.post("/bias")
def bias_endpoint(
    request: ScanRequest,
    context: Annotated[ApplicationContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db_session)],
):
    service = create_scan_service(context, session)
    pairs = [request.pair] if request.pair else context.settings.trading.pairs
    return service.bias_only(pairs)


@app.post("/risk")
def risk_endpoint(
    request: RiskRequest,
    context: Annotated[ApplicationContext, Depends(get_context)],
):
    service = RiskService(context.market_data, context.broker)
    return service.calculate(request.pair, request.risk, request.sl)


@app.get("/sessions")
def sessions_endpoint(
    session_store: Annotated[DatabaseSessionStore, Depends(get_chat_session_store)],
):
    return [item.model_dump(mode="json") for item in session_store.list_recent()]


@app.post("/sessions/resume/{session_id}")
def resume_session_endpoint(
    session_id: str,
    session_store: Annotated[DatabaseSessionStore, Depends(get_chat_session_store)],
):
    return session_store.load_resume_payload(session_id).model_dump(mode="json")


@app.get("/memory")
def memory_endpoint(
    context: Annotated[ApplicationContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db_session)],
    device_token: Annotated[str | None, Depends(get_device_token)] = None,
):
    return {"content": context.create_memory_repository(session, device_token=device_token).get_content()}


@app.post("/memory")
def update_memory_endpoint(
    request: MemoryRequest,
    context: Annotated[ApplicationContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db_session)],
    device_token: Annotated[str | None, Depends(get_device_token)] = None,
):
    content = context.create_memory_repository(session, device_token=device_token).set_content(request.content)
    return {"content": content}


@app.get("/calendar")
def calendar_endpoint(
    context: Annotated[ApplicationContext, Depends(get_context)],
    view: str = "today",
    pair: str | None = None,
):
    service = create_calendar_service(context)
    pairs = [pair] if pair else context.settings.trading.pairs
    return service.get_events(view, pairs).model_dump(mode="json")


@app.post("/api/v1/onboard")
def onboard_endpoint(
    request: OnboardRequest,
    context: Annotated[ApplicationContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db_session)],
) -> OnboardResponse:
    device_token = str(uuid4())
    prophet_md = generate_prophet_md(request)
    record = context.create_user_profile_repository(session).create(
        device_token=device_token,
        display_name=request.display_name,
        experience_level=request.experience_level,
        watchlist=request.watchlist,
        account_balance=request.account_balance,
        risk_pct=request.risk_pct,
        min_rr=request.min_rr,
        sessions=request.sessions,
        prophet_md=prophet_md,
    )
    return OnboardResponse(
        device_token=record.device_token,
        display_name=record.display_name,
        prophet_md_preview=prophet_md[:500],
        message=f"Welcome to Prophet, {record.display_name}. Your profile is ready.",
    )


@app.get("/api/v1/profile")
def profile_endpoint(
    context: Annotated[ApplicationContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db_session)],
    device_token: Annotated[str | None, Depends(get_device_token)],
) -> ProfileResponse:
    if not device_token:
        raise HTTPException(status_code=404, detail="Profile not found")
    record = context.create_user_profile_repository(session).get_by_device_token(device_token)
    if record is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_response(record)


@app.patch("/api/v1/profile")
def update_profile_endpoint(
    request: ProfileUpdateRequest,
    context: Annotated[ApplicationContext, Depends(get_context)],
    session: Annotated[Session, Depends(get_db_session)],
    device_token: Annotated[str | None, Depends(get_device_token)],
) -> ProfileResponse:
    if not device_token:
        raise HTTPException(status_code=404, detail="Profile not found")
    return _profile_response(_update_profile_record(context, session, device_token, request))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
