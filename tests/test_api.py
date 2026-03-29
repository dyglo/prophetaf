import logging
import asyncio
from contextlib import contextmanager
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from hedge_fund.api import ChatRequest, calendar_endpoint, chat, memory_endpoint, sessions_endpoint, update_memory_endpoint, MemoryRequest
from hedge_fund.chat.models import ChatResponse, ChatTurn
from hedge_fund.chat.session_store import DatabaseSessionStore, SessionNotFoundError
from hedge_fund.cli.bootstrap import ApplicationContext
from hedge_fund.domain.exceptions import ConfigurationError
from hedge_fund.integrations.calendar import TwelveDataCalendarClient
from hedge_fund.config.settings import Settings
from hedge_fund.storage.base import Base
from hedge_fund.storage.profile_repository import UserProfileRepository


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def test_database_session_store_round_trips_state_across_instances() -> None:
    factory = _session_factory()
    first = DatabaseSessionStore(factory)
    second = DatabaseSessionStore(factory)

    state = first.create(
        max_context_turns=Settings.load().chat.max_context_turns,
        permission_mode="default",
        model_override="auto",
        append_system_prompt=None,
    )
    first.add_turn(state, ChatTurn(role="user", content="hello"))

    loaded = second.load(state.session.session_id)
    latest = second.load_latest()

    assert loaded.session.session_id == state.session.session_id
    assert loaded.session.turns[-1].content == "hello"
    assert latest.session.session_id == state.session.session_id


def test_database_session_store_populates_archived_session_listing() -> None:
    store = DatabaseSessionStore(_session_factory())
    state = store.create(
        max_context_turns=Settings.load().chat.max_context_turns,
        permission_mode="default",
        model_override=None,
        append_system_prompt=None,
    )
    store.add_turn(state, ChatTurn(role="user", content="hello"))
    state.session.summary = "Covered EURUSD bias and entries."
    state.session.ended_at = state.session.updated_at
    store.finalize(state)

    listing = store.list_recent()
    resume_payload = store.load_resume_payload(state.session.session_id)

    assert listing[0].summary == "Covered EURUSD bias and entries."
    assert resume_payload.messages[-1]["content"] == "hello"


def test_database_session_store_only_archives_on_finalize() -> None:
    store = DatabaseSessionStore(_session_factory())
    state = store.create(
        max_context_turns=Settings.load().chat.max_context_turns,
        permission_mode="default",
        model_override=None,
        append_system_prompt=None,
    )
    store.add_turn(state, ChatTurn(role="user", content="hello"))

    listing = store.list_recent()
    assert listing[0].id == state.session.session_id
    assert listing[0].summary is None

    state.session.summary = "Finalized"
    state.session.ended_at = state.session.updated_at
    store.finalize(state)

    assert store.list_recent()[0].summary == "Finalized"


def test_database_session_store_resume_payload_falls_back_to_live_session() -> None:
    store = DatabaseSessionStore(_session_factory())
    state = store.create(
        max_context_turns=Settings.load().chat.max_context_turns,
        permission_mode="default",
        model_override=None,
        append_system_prompt=None,
    )
    store.add_turn(state, ChatTurn(role="user", content="hello"))
    store.add_turn(state, ChatTurn(role="assistant", content="Last response"))

    payload = store.load_resume_payload(state.session.session_id)

    assert payload.id == state.session.session_id
    assert payload.messages[-1]["content"] == "Last response"
    assert payload.summary == "The session discussed hello and finished with Last response"
    assert "Resuming session from" in (payload.recap or "")


def test_database_session_store_generates_resume_summary_when_missing() -> None:
    store = DatabaseSessionStore(_session_factory(), summary_generator=lambda turns: "Discussed EURUSD trend and a possible long setup.")
    state = store.create(
        max_context_turns=Settings.load().chat.max_context_turns,
        permission_mode="default",
        model_override=None,
        append_system_prompt=None,
    )
    store.add_turn(state, ChatTurn(role="user", content="What is the current trend of EURUSD?"))
    store.add_turn(state, ChatTurn(role="assistant", content="EURUSD is bullish."))

    payload = store.load_resume_payload(state.session.session_id)

    assert payload.summary == "Discussed EURUSD trend and a possible long setup."
    assert "Discussed EURUSD trend and a possible long setup." in (payload.recap or "")


def test_database_session_store_raises_domain_specific_miss() -> None:
    store = DatabaseSessionStore(_session_factory())

    with pytest.raises(SessionNotFoundError) as exc_info:
        store.load("missing")

    assert str(exc_info.value) == "missing"


def test_chat_endpoint_returns_full_chat_response_and_closes_runner(monkeypatch) -> None:
    created_runners = []

    class FakeService:
        def process_message(self, state, message):
            assert state.session.turns[-1].content == "Earlier answer"
            return ChatResponse(
                session_id=state.session.session_id,
                message="Closing chat session.",
                metadata={"view": "help_menu", "commands": [("/help", "Show the command palette")]},
                should_exit=True,
            )

    class FakeRunner:
        def __init__(self, context, cwd=None, session_store=None, repository=None) -> None:
            self.session_store = session_store
            self.closed = False
            created_runners.append(self)

        def build_service(self, model_override, append_system_prompt, device_token=None):
            return FakeService()

        def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.settings = Settings.load()
            self.logger = logging.getLogger("test")

        def create_repository(self, session):
            return object()

    monkeypatch.setattr("hedge_fund.api.ChatCommandRunner", FakeRunner)

    store = DatabaseSessionStore(_session_factory())
    response = chat(
        ChatRequest(
            message="/exit",
            history=[
                {"role": "user", "content": "Hello", "metadata": {}},
                {"role": "assistant", "content": "Earlier answer", "metadata": {}},
            ],
        ),
        FakeContext(),
        object(),
        store,
    )

    assert response.should_exit is True
    assert response.metadata["view"] == "help_menu"
    assert response.message == "Closing chat session."
    assert created_runners[0].closed is True


def test_chat_endpoint_streams_sse_events(monkeypatch) -> None:
    created_runners = []
    created_repositories = []

    class FakeService:
        def process_message(self, state, message, authorize_mutation=None, event_sink=None, stream_handler=None):
            assert stream_handler is not None
            assert event_sink is not None
            event_sink.emit_plan("1. Check the watchlist.\n2. Compare the strongest setups.")
            event_sink.update_status("Scanning the watchlist...")
            event_sink.emit_reasoning("XAUUSD is starting to stand out, so I am checking it more closely.")
            stream_handler("Hello ")
            stream_handler("world")
            return ChatResponse(session_id=state.session.session_id, message="Hello world")

    class FakeRunner:
        def __init__(self, context, cwd=None, session_store=None, repository=None) -> None:
            self.session_store = session_store
            self.closed = False
            self.repository = repository
            created_runners.append(self)

        def build_service(self, model_override, append_system_prompt, device_token=None):
            return FakeService()

        def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self) -> None:
            self.settings = Settings.load()
            self.logger = logging.getLogger("test")
            self._worker_session = object()

        def create_repository(self, session):
            created_repositories.append(session)
            return session

        @contextmanager
        def session_scope(self):
            yield self._worker_session

    monkeypatch.setattr("hedge_fund.api.ChatCommandRunner", FakeRunner)

    store = DatabaseSessionStore(_session_factory())
    request_session = object()
    response = chat(ChatRequest(message="Hello", stream=True), FakeContext(), request_session, store)

    assert created_runners == []

    chunks = []

    async def collect() -> None:
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    asyncio.run(collect())

    combined = "".join(chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks)
    assert "event: plan" in combined
    assert '"message": "1. Check the watchlist.\\n2. Compare the strongest setups."' in combined
    assert "event: step" in combined
    assert '"message": "Scanning the watchlist..."' in combined
    assert "event: reasoning" in combined
    assert "XAUUSD is starting to stand out" in combined
    assert "event: message" in combined
    assert '"delta": "Hello "' in combined
    assert "event: done" in combined
    assert combined.index("event: plan") < combined.index("event: step")
    assert len(created_runners) == 1
    assert created_runners[0].closed is True
    assert created_repositories[0] is not request_session


def test_chat_endpoint_prefers_history_over_messages(monkeypatch) -> None:
    observed = {}

    class FakeService:
        def process_message(self, state, message):
            observed["turns"] = [(turn.role, turn.content) for turn in state.session.turns]
            return ChatResponse(session_id=state.session.session_id, message="ok")

    class FakeRunner:
        def __init__(self, context, cwd=None, session_store=None, repository=None) -> None:
            pass

        def build_service(self, model_override, append_system_prompt, device_token=None):
            return FakeService()

        def close(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.settings = Settings.load()
            self.logger = logging.getLogger("test")

        def create_repository(self, session):
            return object()

    monkeypatch.setattr("hedge_fund.api.ChatCommandRunner", FakeRunner)

    store = DatabaseSessionStore(_session_factory())
    chat(
        ChatRequest(
            message="Follow-up",
            history=[
                {"role": "user", "content": "History user", "metadata": {}},
                {"role": "assistant", "content": "History answer", "metadata": {}},
            ],
            messages=[
                {"role": "user", "content": "Legacy user", "metadata": {}},
                {"role": "assistant", "content": "Legacy answer", "metadata": {}},
            ],
        ),
        FakeContext(),
        object(),
        store,
    )

    assert observed["turns"] == [("user", "History user"), ("assistant", "History answer")]


def test_session_scope_rolls_back_before_close_on_exception() -> None:
    events = []

    class FakeSession:
        def rollback(self) -> None:
            events.append("rollback")

        def close(self) -> None:
            events.append("close")

    context = ApplicationContext.__new__(ApplicationContext)
    context.create_session = lambda: FakeSession()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        with ApplicationContext.session_scope(context):
            raise RuntimeError("boom")

    assert events == ["rollback", "close"]


def test_memory_and_calendar_endpoints_use_context_repositories() -> None:
    class FakeMemoryRepository:
        def __init__(self) -> None:
            self.content = "- Rule"

        def get_content(self):
            return self.content

        def set_content(self, content: str):
            self.content = content
            return self.content

    class FakeCalendarService:
        def get_events(self, view: str, pairs: list[str]):
            return type(
                "CalendarPayload",
                (),
                {
                    "model_dump": lambda self, mode="json": {
                        "view": view,
                        "provider": "fake",
                        "events": [{"event_name": "CPI"}],
                        "warnings": [],
                    }
                },
            )()

    context = ApplicationContext.__new__(ApplicationContext)
    context.settings = Settings.load()
    context.create_memory_repository = lambda session, device_token=None: FakeMemoryRepository()  # type: ignore[method-assign]
    context.calendar = object()

    memory = memory_endpoint(context, object())
    updated = update_memory_endpoint(MemoryRequest(content="- New rule"), context, object())

    monkeypatch_calendar = FakeCalendarService()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("hedge_fund.api.create_calendar_service", lambda ctx: monkeypatch_calendar)
    try:
        calendar = calendar_endpoint(context, "today", "XAUUSD")
    finally:
        monkeypatch.undo()

    assert memory["content"] == "- Rule"
    assert updated["content"] == "- New rule"
    assert calendar["events"][0]["event_name"] == "CPI"


def test_memory_endpoints_use_profile_specific_memory_when_device_token_is_present() -> None:
    factory = _session_factory()
    logger = logging.getLogger("test")
    with factory() as seed_session:
        UserProfileRepository(seed_session, logger).create(
            device_token="device-123",
            display_name="Tafar",
            experience_level="beginner",
            watchlist=["XAUUSD"],
            account_balance=10000,
            risk_pct=1.0,
            min_rr="1:2",
            sessions=["London"],
            prophet_md="- Initial profile rule",
        )

    class FakeContext:
        def __init__(self) -> None:
            self.settings = Settings.load()

        def create_memory_repository(self, session, device_token=None):
            from hedge_fund.storage.chat_repository import ProphetMemoryRepository

            return ProphetMemoryRepository(session, logger, device_token=device_token)

    context = FakeContext()
    with factory() as session:
        memory = memory_endpoint(context, session, "device-123")
        updated = update_memory_endpoint(MemoryRequest(content="- Updated profile rule"), context, session, "device-123")

    with factory() as verify_session:
        record = UserProfileRepository(verify_session, logger).get_by_device_token("device-123")

    assert memory["content"] == "- Initial profile rule"
    assert updated["content"] == "- Updated profile rule"
    assert record.prophet_md == "- Updated profile rule"


def test_calendar_endpoint_returns_clean_payload_when_provider_is_unavailable(monkeypatch) -> None:
    class FakeCalendarService:
        def get_events(self, view: str, pairs: list[str]):
            return type(
                "CalendarPayload",
                (),
                {
                    "model_dump": lambda self, mode="json": {
                        "view": view,
                        "provider": "twelvedata",
                        "events": [],
                        "warnings": [{"pair": "calendar", "message": "Prophet calendar is unavailable."}],
                    }
                },
            )()

    context = ApplicationContext.__new__(ApplicationContext)
    context.settings = Settings.load()
    context.calendar = None

    monkeypatch.setattr("hedge_fund.api.create_calendar_service", lambda ctx: FakeCalendarService())

    payload = calendar_endpoint(context, "today", None)

    assert payload["provider"] == "twelvedata"
    assert payload["warnings"][0]["message"] == "Prophet calendar is unavailable."


def test_sessions_endpoint_returns_archived_listing() -> None:
    store = DatabaseSessionStore(_session_factory())
    state = store.create(
        max_context_turns=Settings.load().chat.max_context_turns,
        permission_mode="default",
        model_override=None,
        append_system_prompt=None,
    )
    state.session.summary = "Summary"
    state.session.ended_at = state.session.updated_at
    store.finalize(state)

    payload = sessions_endpoint(store)

    assert payload[0]["summary"] == "Summary"


def test_twelve_data_calendar_requires_api_key() -> None:
    client = TwelveDataCalendarClient(None, 5.0, logging.getLogger("test"))

    with pytest.raises(ConfigurationError) as exc_info:
        client.fetch_events(date(2026, 3, 9), date(2026, 3, 9))

    assert "TWELVE_DATA_API_KEY" in str(exc_info.value)


def test_twelve_data_calendar_reports_missing_macro_endpoint() -> None:
    client = TwelveDataCalendarClient("key", 5.0, logging.getLogger("test"))

    monkeypatch = pytest.MonkeyPatch()

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, path, params=None):
            payloads = {
                "/earnings_calendar": {"data": [{"date": "2026-03-09", "symbol": "AAPL", "name": "Apple"}]},
                "/dividends_calendar": {"data": []},
                "/splits_calendar": {"data": []},
                "/ipo_calendar": {"data": []},
            }
            return FakeResponse(payloads[path])

    monkeypatch.setattr("hedge_fund.integrations.calendar.twelvedata.httpx.Client", FakeClient)
    try:
        events = client.fetch_events(date(2026, 3, 9), date(2026, 3, 9))
    finally:
        monkeypatch.undo()

    assert events[0].event_name == "AAPL Earnings (Apple)"
    assert events[0].source == "Twelve Data"


def test_application_context_calendar_provider_failure_does_not_crash(monkeypatch) -> None:
    context = ApplicationContext.__new__(ApplicationContext)
    context.settings = Settings.load()
    context.env = type("Env", (), {"twelvedata_api_key": None})()
    context.web_search = object()
    context.logger = logging.getLogger("test")

    monkeypatch.setattr(
        "hedge_fund.cli.bootstrap.build_calendar_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConfigurationError("missing tavily")),
    )

    assert context._create_calendar_provider() is None
