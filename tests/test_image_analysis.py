from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hedge_fund.api import app, get_context
from hedge_fund.chat.models import ChatResponse
from hedge_fund.chat.service import ChatService
from hedge_fund.chat.session_store import SessionStore
from hedge_fund.chat.scratchpad import ScratchpadManager
from hedge_fund.chat.config_manager import ConfigManager
from hedge_fund.config.settings import Settings
from hedge_fund.storage.base import Base


class FakeService:
    def __init__(self, observed: dict) -> None:
        self.observed = observed

    def process_message(self, state, message, authorize_mutation=None, event_sink=None, stream_handler=None, image_attachments=None):
        self.observed["message"] = message
        self.observed["image_attachments"] = image_attachments
        return ChatResponse(session_id=state.session.session_id, message="ok")


class FakeRunner:
    def __init__(self, context, cwd=None, session_store=None, repository=None) -> None:
        self.context = context
        self.session_store = session_store
        self.repository = repository
        self.closed = False

    def build_service(self, model_override, append_system_prompt, device_token=None):
        return FakeService(self.context.observed)

    def close(self) -> None:
        self.closed = True


def _chat_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(engine)
    observed: dict = {}

    class FakeContext:
        def __init__(self) -> None:
            self.settings = Settings.load()
            self.env = SimpleNamespace(openai_api_key="key", gemini_api_key="key")
            self.logger = logging.getLogger("test.image-analysis")
            self.session_factory = SessionLocal
            self.observed = observed

        @contextmanager
        def session_scope(self):
            session = SessionLocal()
            try:
                yield session
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        def create_repository(self, session):
            return object()

    monkeypatch.setattr("hedge_fund.api.ChatCommandRunner", FakeRunner)
    app.dependency_overrides[get_context] = lambda: FakeContext()
    return TestClient(app), observed


class FakeLanguage:
    def __init__(self) -> None:
        self.settings = Settings.load()


class FakeScanService:
    def bias_only(self, pairs):
        return []

    def scan(self, pairs):
        return SimpleNamespace(biases=[], setups=[], ai_analysis=[])


class FakeRiskService:
    def calculate(self, pair: str, risk_pct: float, sl_pips: int):
        return None


class FakeReverseRiskService:
    def calculate(self, pair: str, lot_size: float, sl_pips: int):
        return None


class CapturingAgentRuntime:
    def __init__(self) -> None:
        self.model_override = None
        self.history_messages = None
        self.system_prompt = None

    def run(
        self,
        *,
        user_message,
        system_prompt,
        tools,
        scratchpad,
        artifacts,
        event_sink=None,
        history_messages=None,
        stream_handler=None,
        prophet_md="",
        plan_handler=None,
        reasoning_handler=None,
    ):
        self.history_messages = history_messages
        self.system_prompt = system_prompt
        return SimpleNamespace(message="ok", metadata={})


def _agent_service(tmp_path: Path):
    settings = Settings.load()
    config_path = tmp_path / "config.yaml"
    repo_config = Path(__file__).resolve().parents[1] / "config.yaml"
    config_path.write_text(repo_config.read_text(encoding="utf-8"), encoding="utf-8")
    session_store = SessionStore(tmp_path)
    state = session_store.create(
        max_context_turns=settings.chat.max_context_turns,
        permission_mode="accept_edits",
        model_override=None,
        append_system_prompt=None,
    )
    runtime = CapturingAgentRuntime()
    service = ChatService(
        settings,
        FakeScanService(),
        FakeRiskService(),
        FakeReverseRiskService(),
        FakeLanguage(),
        ConfigManager(config_path),
        session_store,
        agent_runtime=runtime,
        scratchpad_manager=ScratchpadManager(tmp_path, settings.agent),
        memory_repository=None,
        user_profile_repository=None,
        calendar_service=None,
    )
    return service, state, runtime


def test_chat_endpoint_accepts_valid_image_payload(monkeypatch) -> None:
    client, observed = _chat_client(monkeypatch)

    response = client.post(
        "/chat",
        json={"message": "Analyse this chart", "image_b64": "abc123", "media_type": "image/png"},
    )

    assert response.status_code == 200
    assert observed["image_attachments"] == [{"base64": "abc123", "media_type": "image/png"}]
    app.dependency_overrides.clear()


def test_chat_endpoint_rejects_image_without_media_type(monkeypatch) -> None:
    client, _ = _chat_client(monkeypatch)

    response = client.post(
        "/chat",
        json={"message": "Analyse this chart", "image_b64": "abc123"},
    )

    assert response.status_code == 422
    app.dependency_overrides.clear()


def test_chat_endpoint_treats_empty_image_string_as_text_only(monkeypatch) -> None:
    client, observed = _chat_client(monkeypatch)

    response = client.post(
        "/chat",
        json={"message": "Text only please", "image_b64": "   ", "media_type": "image/png"},
    )

    assert response.status_code == 200
    assert observed["image_attachments"] is None
    app.dependency_overrides.clear()


def test_single_image_builds_one_image_block_before_text(tmp_path: Path) -> None:
    service, state, runtime = _agent_service(tmp_path)

    response = service.process_message(
        state,
        "What do you see here?",
        image_attachments=[{"base64": "abc123", "media_type": "image/png"}],
    )

    assert response.message == "ok"
    assert runtime.history_messages[-1]["role"] == "user"
    assert runtime.history_messages[-1]["content"][0] == {
        "type": "image",
        "base64": "abc123",
        "mime_type": "image/png",
    }
    assert runtime.history_messages[-1]["content"][1]["type"] == "text"
    assert "You are analysing a trading chart image provided by the user." in runtime.history_messages[-1]["content"][1]["text"]
    assert "What do you see here?" in runtime.history_messages[-1]["content"][1]["text"]
    assert "what prophet sees" in runtime.system_prompt.lower()


def test_two_images_build_two_image_blocks_before_text(tmp_path: Path) -> None:
    service, state, runtime = _agent_service(tmp_path)

    response = service.process_message(
        state,
        "Check alignment",
        image_attachments=[
            {"base64": "abc123", "media_type": "image/png"},
            {"base64": "def456", "media_type": "image/jpeg"},
        ],
    )

    assert response.message == "ok"
    assert runtime.history_messages[-1]["content"][:2] == [
        {"type": "image", "base64": "abc123", "mime_type": "image/png"},
        {"type": "image", "base64": "def456", "mime_type": "image/jpeg"},
    ]
    assert runtime.history_messages[-1]["content"][2]["type"] == "text"
    assert "two trading chart images for multi-timeframe analysis" in runtime.history_messages[-1]["content"][2]["text"]
    assert "Check alignment" in runtime.history_messages[-1]["content"][2]["text"]
