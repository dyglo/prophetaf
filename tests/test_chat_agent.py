import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages import AIMessageChunk

from hedge_fund.chat.agent_runtime import AgentArtifacts, AgentRuntime
import hedge_fund.chat.agent_runtime as agent_runtime_module
from hedge_fund.chat.agent_models import AgentModelFactory
from hedge_fund.chat.agent_tools import AgentToolContext
from hedge_fund.chat.config_manager import ConfigManager
from hedge_fund.chat.models import ChatTurn
from hedge_fund.chat.service import ChatService, ReverseRiskService
from hedge_fund.chat.session_store import SessionStore
from hedge_fund.chat.scratchpad import ScratchpadManager
from hedge_fund.config.environment import EnvironmentSettings
from hedge_fund.config.settings import Settings
from hedge_fund.domain.models import BiasResult, RiskCalculation, SetupScanResult
from hedge_fund.services.scan_service import ScanResultBundle


class FakeLanguage:
    def __init__(self) -> None:
        self.settings = Settings.load()


class FakeScanService:
    def __init__(self, fail_bias: bool = False) -> None:
        self.settings = Settings.load()
        self.fail_bias = fail_bias

    def bias_only(self, pairs):
        if self.fail_bias:
            raise ValueError("bias unavailable")
        return [
            BiasResult(
                pair=pairs[0],
                bias="Bullish",
                structure="HH/HL",
                key_level=1.1,
                key_level_type="swing_low",
            )
        ]

    def scan(self, pairs):
        return ScanResultBundle(
            biases=self.bias_only(pairs),
            setups=[
                SetupScanResult(
                    pair=pairs[0],
                    fvg_detected=True,
                    fvg_range=None,
                    fib_zone_hit=True,
                    fib_level=0.618,
                    liquidity_sweep=True,
                    sweep_level=1.1,
                    score=8,
                    signals_summary="FVG, Fib",
                    direction="Long",
                    surfaced=True,
                )
            ],
            ai_analysis=[],
        )


class FakeRiskService:
    def calculate(self, pair: str, risk_pct: float, sl_pips: int) -> RiskCalculation:
        return RiskCalculation(
            pair=pair,
            account_balance=10000,
            risk_pct=risk_pct,
            risk_amount=100,
            sl_pips=sl_pips,
            lot_size=0.5,
            tp_1r2=1.2,
            tp_1r3=1.3,
            rr_used=3,
        )


class FakeSearchClient:
    def search(self, query: str):
        return {
            "query": query,
            "summary": "Gold is reacting to dollar weakness.",
            "results": [
                {"title": "Gold climbs", "url": "https://example.com/gold", "snippet": "Gold rose after weaker data."}
            ],
        }


class FakeMemoryRepository:
    def __init__(self) -> None:
        self.content = "- Avoid GBPUSD during BOE week"

    def get_content(self) -> str:
        return self.content

    def add_rule(self, rule: str, max_characters: int):
        self.content = f"{self.content}\n- {rule}"
        return self.content, True

    def forget_rule(self, rule: str) -> str:
        self.content = ""
        return self.content


class FakeCalendarPayload:
    def __init__(self) -> None:
        self.events = [type("Event", (), {"event_name": "CPI", "currency": "USD", "time_utc": "13:30"})()]
        self.warnings = [type("Warning", (), {"pair": "XAUUSD", "message": "USD CPI affects XAUUSD."})()]

    def model_dump(self, mode="json"):
        return {
            "view": "today",
            "provider": "fake",
            "events": [{"event_name": "CPI", "currency": "USD", "time_utc": "13:30"}],
            "warnings": [{"pair": "XAUUSD", "message": "USD CPI affects XAUUSD."}],
        }


class FakeCalendarService:
    def get_events(self, view: str, pairs: list[str]):
        return FakeCalendarPayload()


class _MarketData:
    def get_price(self, pair: str):
        return 2900.0 if pair == "XAUUSD" else 1.25


class _Broker:
    def get_account_balance(self):
        return 10000.0

    def get_instrument_metadata(self, pair: str):
        return {"pipLocation": -4}


def _config_manager(tmp_path):
    config_path = tmp_path / "config.yaml"
    repo_config = Path(__file__).resolve().parents[1] / "config.yaml"
    config_path.write_text(repo_config.read_text(encoding="utf-8"), encoding="utf-8")
    return ConfigManager(config_path)


def _state(tmp_path):
    store = SessionStore(tmp_path)
    state = store.create(
        max_context_turns=Settings.load().chat.max_context_turns,
        permission_mode="accept_edits",
        model_override=None,
        append_system_prompt=None,
    )
    return store, state


def _scratchpad_entries(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_agent_tool_returns_bias_payload_and_logs_scratchpad(tmp_path) -> None:
    store, state = _state(tmp_path)
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session(state.session.session_id)
    artifacts = AgentArtifacts()
    context = AgentToolContext(
        settings=Settings.load(),
        state=state,
        scan_service=FakeScanService(),
        risk_service=FakeRiskService(),
        reverse_risk_service=ReverseRiskService(_MarketData(), _Broker()),
        config_manager=_config_manager(tmp_path),
        search_client=FakeSearchClient(),
        scratchpad=scratchpad,
        artifacts=artifacts,
        memory_repository=FakeMemoryRepository(),
        calendar_service=FakeCalendarService(),
    )

    tool = next(item for item in context.build_tools() if item.name == "get_market_bias")
    payload = json.loads(tool.invoke({"pair": "Gold"}))

    assert payload["ok"] is True
    assert payload["biases"][0]["pair"] == "XAUUSD"
    entries = _scratchpad_entries(scratchpad.path)
    assert entries[-1]["type"] == "tool_result"
    assert entries[-1]["toolName"] == "get_market_bias"
    assert entries[-1]["args"] == {"pair": "Gold", "all_pairs": False}
    assert entries[-1]["rawResult"]["ok"] is True
    assert "XAUUSD: Bullish" in entries[-1]["llmSummary"]
    assert state.session.context.active_pair == "XAUUSD"


def test_agent_selects_web_search_for_news_queries(tmp_path, monkeypatch) -> None:
    service, state = _agent_service(tmp_path)

    class FakeAgent:
        def __init__(self, tools):
            self.tools = {tool.name: tool for tool in tools}

        def stream(self, payload, config=None, stream_mode=None):
            query = "gold news today"
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": "web_search", "args": {"query": query}, "id": "call-1", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            result = self.tools["web_search"].invoke({"query": query})
            yield ("updates", {"tools": {"messages": [ToolMessage(content=result, tool_call_id="call-1")]}})
            yield ("updates", {"model": {"messages": [AIMessage(content="Gold is being driven by macro headlines.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: FakeAgent(tools))

    response = service.process_message(state, "Any major news on Gold today?")

    assert "macro headlines" in response.message
    assert response.metadata["web_search"]["query"] == "gold news today"


def test_agent_selects_bias_tool_for_bias_queries(tmp_path, monkeypatch) -> None:
    service, state = _agent_service(tmp_path)

    class FakeAgent:
        def __init__(self, tools):
            self.tools = {tool.name: tool for tool in tools}

        def stream(self, payload, config=None, stream_mode=None):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": "get_market_bias", "args": {"pair": "Gold"}, "id": "call-1", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            result = self.tools["get_market_bias"].invoke({"pair": "Gold"})
            yield ("updates", {"tools": {"messages": [ToolMessage(content=result, tool_call_id="call-1")]}})
            yield ("updates", {"model": {"messages": [AIMessage(content="Gold bias is bullish.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: FakeAgent(tools))

    response = service.process_message(state, "What's the bias on Gold?")

    assert response.biases[0].pair == "XAUUSD"
    assert "bullish" in response.message.lower()


def test_agent_returns_partial_result_when_max_steps_are_exceeded(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    artifacts = AgentArtifacts(summaries=["Bias: XAUUSD Bullish"])

    class RecursingAgent:
        def stream(self, payload, config=None, stream_mode=None):
            raise agent_runtime_module.GraphRecursionError("recursion")

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: RecursingAgent())

    result = runtime.run("Need help", "system", [], scratchpad, artifacts)

    assert result.metadata["partial"] is True
    assert "step limit" in result.message


def test_agent_handles_tool_failure_without_crashing(tmp_path, monkeypatch) -> None:
    service, state = _agent_service(tmp_path, fail_bias=True)

    class FakeAgent:
        def __init__(self, tools):
            self.tools = {tool.name: tool for tool in tools}

        def stream(self, payload, config=None, stream_mode=None):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": "get_market_bias", "args": {"pair": "Gold"}, "id": "call-1", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            result = self.tools["get_market_bias"].invoke({"pair": "Gold"})
            yield ("updates", {"tools": {"messages": [ToolMessage(content=result, tool_call_id="call-1")]}})
            yield ("updates", {"model": {"messages": [AIMessage(content="Bias tool failed, but the session is still live.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: FakeAgent(tools))

    response = service.process_message(state, "What's the bias on Gold?")

    assert "still live" in response.message
    scratchpad_path = tmp_path / ".prophet" / "scratchpad" / f"{state.session.session_id}.jsonl"
    assert '"ok": false' in scratchpad_path.read_text(encoding="utf-8").lower()


def _agent_service(tmp_path, fail_bias: bool = False):
    settings = Settings.load()
    env = EnvironmentSettings(database_url="sqlite://", openai_api_key="key")
    config_manager = _config_manager(tmp_path)
    session_store, state = _state(tmp_path)
    service = ChatService(
        settings,
        FakeScanService(fail_bias=fail_bias),
        FakeRiskService(),
        ReverseRiskService(_MarketData(), _Broker()),
        FakeLanguage(),
        config_manager,
        session_store,
        agent_runtime=AgentRuntime(settings, env, logging.getLogger("test")),
        scratchpad_manager=ScratchpadManager(tmp_path, settings.agent),
        search_client=FakeSearchClient(),
        memory_repository=FakeMemoryRepository(),
        calendar_service=FakeCalendarService(),
    )
    return service, state


def _seed_verbose_history(state) -> None:
    noisy_tool_summary = "; ".join(
        f"Bias: EURUSD Bullish {index}"
        for index in range(1, 8)
    )
    assistant_text = (
        "EURUSD is bullish across the higher timeframe and the London session remains constructive. "
        "Wait for confirmation at support before entering."
    ) * 3
    state.session.turns = [
        ChatTurn(role="user", content="What is the current trend of EURUSD?"),
        ChatTurn(
            role="assistant",
            content=assistant_text,
            metadata={"tool_summaries": [noisy_tool_summary]},
        ),
        ChatTurn(role="user", content="Anything I should avoid?"),
        ChatTurn(
            role="assistant",
            content="Avoid chasing entries into resistance and wait for structure to confirm.",
            metadata={"tool_summaries": ["Watchlist: XAUUSD, EURUSD, GBPUSD, USDJPY, USDCHF"]},
        ),
    ]


def test_agent_ignores_stream_updates_without_messages(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    artifacts = AgentArtifacts(summaries=["Bias: XAUUSD Bullish"])

    class SparseAgent:
        def stream(self, payload, config=None, stream_mode=None):
            yield ("updates", {"metadata": {"step": {"ignored": True}}})
            yield ("updates", {"model": {"messages": [AIMessage(content="Final answer.") ]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: SparseAgent())

    result = runtime.run("Need help", "system", [], scratchpad, artifacts)

    assert result.message == "Final answer."


def test_agent_emits_reasoning_before_and_after_tool_calls(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    artifacts = AgentArtifacts()
    events = []

    class Sink:
        def update_status(self, message: str) -> None:
            events.append(("step", message))

        def emit_reasoning(self, message: str) -> None:
            events.append(("reasoning", message))

    class ToolAgent:
        def stream(self, payload, config=None, stream_mode=None):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": "get_market_bias", "args": {"pair": "XAUUSD"}, "id": "call-1", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            ToolMessage(content='{"ok": true, "summary": "XAUUSD bias is bullish."}', tool_call_id="call-1"),
                        ]
                    }
                },
            )
            yield ("updates", {"model": {"messages": [AIMessage(content="Final answer.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: ToolAgent())

    runtime.run(
        "Need help",
        "system",
        [],
        scratchpad,
        artifacts,
        event_sink=Sink(),
        reasoning_handler=lambda tool_name, phase, payload: f"{phase}:{tool_name}:{payload.get('summary', payload.get('pair', ''))}",
    )

    assert ("step", "Reading market structure...") in events
    assert ("reasoning", "before:get_market_bias:XAUUSD") in events
    assert ("reasoning", "after:get_market_bias:XAUUSD bias is bullish.") in events


def test_agent_emits_plan_before_tool_calls_for_multi_tool_runs(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    artifacts = AgentArtifacts()
    events = []

    class Sink:
        def update_status(self, message: str) -> None:
            events.append(("step", message))

        def emit_plan(self, message: str) -> None:
            events.append(("plan", message))

        def emit_reasoning(self, message: str) -> None:
            events.append(("reasoning", message))

    class ToolAgent:
        def stream(self, payload, config=None, stream_mode=None):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {"name": "get_market_bias", "args": {"pair": "XAUUSD"}, "id": "call-1", "type": "tool_call"},
                                    {"name": "get_economic_calendar", "args": {"view": "today"}, "id": "call-2", "type": "tool_call"},
                                ],
                            )
                        ]
                    }
                },
            )
            yield ("updates", {"model": {"messages": [AIMessage(content="Final answer.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: ToolAgent())

    runtime.run(
        "Compare bias and news risk on Gold",
        "system",
        [],
        scratchpad,
        artifacts,
        event_sink=Sink(),
        plan_handler=lambda user_message, history_messages: "1. Check market bias.\n2. Check near-term news risk.",
    )

    assert events[0] == ("plan", "1. Check market bias.\n2. Check near-term news risk.")
    assert any(event == ("step", "Reading market structure...") for event in events)
    entries = _scratchpad_entries(scratchpad.path)
    assert entries[0]["type"] == "init"
    assert entries[0]["plan"] == "1. Check market bias.\n2. Check near-term news risk."


def test_agent_skips_plan_event_for_single_tool_queries(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    artifacts = AgentArtifacts()
    events = []

    class Sink:
        def update_status(self, message: str) -> None:
            events.append(("step", message))

        def emit_plan(self, message: str) -> None:
            events.append(("plan", message))

        def emit_reasoning(self, message: str) -> None:
            events.append(("reasoning", message))

    class SingleToolAgent:
        def stream(self, payload, config=None, stream_mode=None):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": "get_market_bias", "args": {"pair": "XAUUSD"}, "id": "call-1", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            yield ("updates", {"model": {"messages": [AIMessage(content="Final answer.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: SingleToolAgent())

    runtime.run(
        "What is the bias on Gold?",
        "system",
        [],
        scratchpad,
        artifacts,
        event_sink=Sink(),
        plan_handler=lambda user_message, history_messages: None,
    )

    assert not any(kind == "plan" for kind, _ in events)
    entries = _scratchpad_entries(scratchpad.path)
    assert entries[0]["type"] == "init"
    assert entries[0]["plan"] is None


def test_agent_appends_validation_flags_when_prophet_rules_are_present(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    now = datetime.now(tz=UTC)
    artifacts = AgentArtifacts(
        biases=[BiasResult(pair="XAUUSD", bias="Ranging", structure="flat", key_level=2900.0, key_level_type="swing_low")],
        setups=[
            SetupScanResult(pair="XAUUSD", score=5, direction="Neutral", fvg_detected=False, fib_zone_hit=False, liquidity_sweep=False, signals_summary="A", surfaced=True),
            SetupScanResult(pair="EURUSD", score=5, direction="Neutral", fvg_detected=False, fib_zone_hit=False, liquidity_sweep=False, signals_summary="B", surfaced=True),
            SetupScanResult(pair="GBPUSD", score=5, direction="Neutral", fvg_detected=False, fib_zone_hit=False, liquidity_sweep=False, signals_summary="C", surfaced=True),
            SetupScanResult(pair="USDJPY", score=5, direction="Neutral", fvg_detected=False, fib_zone_hit=False, liquidity_sweep=False, signals_summary="D", surfaced=True),
        ],
        metadata={
            "calendar": {
                "events": [
                    {
                        "date": now.strftime("%Y-%m-%d"),
                        "time_utc": now.strftime("%H:%M"),
                        "currency": "USD",
                        "event_name": "US CPI",
                        "impact": "High",
                    }
                ]
            }
        },
    )

    class FinalAgent:
        def stream(self, payload, config=None, stream_mode=None):
            yield ("updates", {"model": {"messages": [AIMessage(content="Final answer.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: FinalAgent())
    monkeypatch.setattr("hedge_fund.chat.utils.current_session_status", lambda sessions, now=None: {"current_session": "Asia", "status": "Asia is open now."})

    result = runtime.run(
        "Need a read on Gold",
        "system",
        [],
        scratchpad,
        artifacts,
        prophet_md="# Rules\n- anything",
    )

    assert "Validation Flags:" in result.message
    assert "Asia session trading is active" in result.message
    assert "High-impact news (US CPI) is within 15 minutes" in result.message
    assert "ranging with no clear directional bias" in result.message
    assert "More than 3 setups were surfaced" in result.message


def test_agent_skips_validation_when_prophet_rules_are_empty(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    artifacts = AgentArtifacts(
        biases=[BiasResult(pair="XAUUSD", bias="Ranging", structure="flat", key_level=2900.0, key_level_type="swing_low")]
    )

    class FinalAgent:
        def stream(self, payload, config=None, stream_mode=None):
            yield ("updates", {"model": {"messages": [AIMessage(content="Final answer.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: FinalAgent())

    result = runtime.run(
        "Need a read on Gold",
        "system",
        [],
        scratchpad,
        artifacts,
        prophet_md="",
    )

    assert result.message == "Final answer."


def test_agent_runtime_streams_trade_plan_narrative_then_block(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    artifacts = AgentArtifacts()
    streamed = []

    trade_plan_payload = {
        "ok": True,
        "trade_plan": {
            "pair": "XAUUSD",
            "direction": "LONG",
            "entry": 2900.0,
            "stop_loss": 2890.0,
            "sl_distance": 10.0,
            "tp1": 2920.0,
            "tp2": 2930.0,
            "rr_ratio_tp1": "1:2",
            "rr_ratio_tp2": "1:3",
            "lot_size": 0.1,
            "risk_amount": 100.0,
            "risk_pct": 1.0,
            "tp2_reward": 300.0,
            "setup_type": "FVG + Fib 0.618",
            "session": "London",
            "confluence_score": 8,
            "rule_checks": [
                {"rule": "Risk within limit", "passed": True, "detail": "Risk 1.0% is within the 0.5-1% limit"},
            ],
            "narrative": "Here is your trade plan.",
            "formatted_block": "◆ PROPHET - TRADE PLAN",
        },
        "summary": "Trade plan ready.",
    }

    class ToolAgent:
        def stream(self, payload, config=None, stream_mode=None):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": "generate_trade_plan", "args": {"pair": "XAUUSD"}, "id": "call-1", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            ToolMessage(content=json.dumps(trade_plan_payload), tool_call_id="call-1"),
                        ]
                    }
                },
            )
            yield ("messages", (AIMessageChunk(content="Ignored model text"), {}))
            yield ("updates", {"model": {"messages": [AIMessage(content="Ignored final answer.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: ToolAgent())

    result = runtime.run(
        "Plan this trade",
        "system",
        [],
        scratchpad,
        artifacts,
        stream_handler=streamed.append,
    )

    assert "".join(streamed) == "Here is your trade plan.\n\n◆ PROPHET - TRADE PLAN"
    assert result.message == "Here is your trade plan.\n\n◆ PROPHET - TRADE PLAN"
    assert artifacts.metadata["trade_plan"]["pair"] == "XAUUSD"


def test_agent_runtime_suppresses_failed_trade_plan_retry_reasoning(tmp_path, monkeypatch) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session("session123")
    artifacts = AgentArtifacts()
    events = []
    streamed = []

    failed_payload = {
        "ok": False,
        "tool": "generate_trade_plan",
        "error": "Unrecognised direction 'bullish'. Use LONG, SHORT, BUY, or SELL.",
    }
    successful_payload = {
        "ok": True,
        "trade_plan": {
            "pair": "XAUUSD",
            "direction": "LONG",
            "entry": 2900.0,
            "stop_loss": 2890.0,
            "sl_distance": 10.0,
            "tp1": 2920.0,
            "tp2": 2930.0,
            "rr_ratio_tp1": "1:2",
            "rr_ratio_tp2": "1:3",
            "lot_size": 0.1,
            "risk_amount": 100.0,
            "risk_pct": 1.0,
            "tp2_reward": 300.0,
            "setup_type": "FVG + Fib 0.618",
            "session": "London",
            "confluence_score": 8,
            "rule_checks": [
                {"rule": "Risk within limit", "passed": True, "detail": "Risk 1.0% is within the 0.5-1% limit"},
            ],
            "narrative": "Here is your trade plan.",
            "formatted_block": "◆ PROPHET - TRADE PLAN",
        },
        "summary": "Trade plan ready.",
    }

    class Sink:
        def update_status(self, message: str) -> None:
            events.append(("step", message))

        def emit_reasoning(self, message: str) -> None:
            events.append(("reasoning", message))

    class RetryingToolAgent:
        def stream(self, payload, config=None, stream_mode=None):
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": "generate_trade_plan", "args": {"pair": "XAUUSD", "direction": "bullish"}, "id": "call-1", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            ToolMessage(content=json.dumps(failed_payload), tool_call_id="call-1"),
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": "generate_trade_plan", "args": {"pair": "XAUUSD", "direction": "buy"}, "id": "call-2", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            yield (
                "updates",
                {
                    "tools": {
                        "messages": [
                            ToolMessage(content=json.dumps(successful_payload), tool_call_id="call-2"),
                        ]
                    }
                },
            )
            yield ("updates", {"model": {"messages": [AIMessage(content="Ignored final answer.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: RetryingToolAgent())

    result = runtime.run(
        "Plan this trade",
        "system",
        [],
        scratchpad,
        artifacts,
        event_sink=Sink(),
        stream_handler=streamed.append,
        reasoning_handler=lambda tool_name, phase, payload: f"{phase}:{tool_name}:{payload.get('error', payload.get('summary', ''))}",
    )

    assert ("reasoning", "after:generate_trade_plan:Unrecognised direction 'bullish'. Use LONG, SHORT, BUY, or SELL.") not in events
    assert ("reasoning", "after:generate_trade_plan:Trade plan ready.") in events
    assert "".join(streamed) == "Here is your trade plan.\n\n◆ PROPHET - TRADE PLAN"
    assert result.message == "Here is your trade plan.\n\n◆ PROPHET - TRADE PLAN"


def test_agent_stream_text_ignores_tool_call_messages(tmp_path) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    message = AIMessage(content="internal", tool_calls=[{"id": "call-1", "name": "scan_setups", "args": {}, "type": "tool_call"}])

    assert runtime._stream_text((message, {})) == ""


def test_agent_stream_text_ignores_json_fragments(tmp_path) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    message = AIMessageChunk(content='{"ok": true, "tool": "scan_setups"}')

    assert runtime._stream_text((message, {})) == ""


def test_agent_stream_text_keeps_bracketed_human_text(tmp_path) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    message = AIMessageChunk(content="[EURUSD] Long continuation is valid above the session low.")

    assert runtime._stream_text((message, {})) == "[EURUSD] Long continuation is valid above the session low."


def test_agent_stream_text_keeps_unparseable_braced_human_text(tmp_path) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    message = AIMessageChunk(content="{EURUSD: bullish continuation above 1.0800}")

    assert runtime._stream_text((message, {})) == "{EURUSD: bullish continuation above 1.0800}"


def test_agent_stream_text_ignores_non_human_chunks(tmp_path) -> None:
    runtime = AgentRuntime(Settings.load(), EnvironmentSettings(database_url="sqlite://", openai_api_key="key"), logging.getLogger("test"))
    message = ToolMessage(content="final tool payload", tool_call_id="call-1")

    assert runtime._stream_text((message, {})) == ""


def test_agent_model_factory_passes_api_keys_without_mutating_environment(monkeypatch) -> None:
    captured = {}

    class FakeGemini:
        def __init__(self, **kwargs) -> None:
            captured["gemini"] = kwargs

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            captured["openai"] = kwargs

    settings = Settings.load()
    env = EnvironmentSettings(
        database_url="sqlite://",
        gemini_api_key="gem-key",
        openai_api_key="open-key",
    )

    monkeypatch.setattr("hedge_fund.chat.agent_models.ChatGoogleGenerativeAI", FakeGemini)
    monkeypatch.setattr("hedge_fund.chat.agent_models.ChatOpenAI", FakeOpenAI)

    factory = AgentModelFactory(settings, env)
    factory._build("gemini", settings.ai.models.gemini)
    factory._build("openai", settings.ai.models.openai)

    assert captured["gemini"]["google_api_key"] == "gem-key"
    assert captured["openai"]["api_key"] == "open-key"


def test_agent_runtime_reuses_history_for_follow_up_without_new_tools(tmp_path, monkeypatch) -> None:
    service, state = _agent_service(tmp_path)
    observed = {"tool_calls": 0, "messages": []}

    class HistoryAgent:
        def stream(self, payload, config=None, stream_mode=None):
            observed["messages"].append(payload["messages"])
            last_message = payload["messages"][-1]["content"]
            if last_message == "What is the current trend of EURUSD?":
                yield (
                    "updates",
                    {
                        "model": {
                            "messages": [
                                AIMessage(
                                    content="",
                                    tool_calls=[{"name": "get_market_bias", "args": {"pair": "EURUSD"}, "id": "call-1", "type": "tool_call"}],
                                )
                            ]
                        }
                    },
                )
                observed["tool_calls"] += 1
                yield ("updates", {"model": {"messages": [AIMessage(content="EURUSD is bullish.")]}})
                return
            yield ("updates", {"model": {"messages": [AIMessage(content="EURUSD is still bullish, so only consider long entries on confirmation.")]}})

    monkeypatch.setattr("hedge_fund.chat.agent_runtime.AgentModelFactory.candidates", lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()])
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: HistoryAgent())

    first = service.process_message(state, "What is the current trend of EURUSD?")
    second = service.process_message(state, "Can I enter long for this trend?")

    assert first.message == "EURUSD is bullish."
    assert "EURUSD is still bullish" in second.message
    assert observed["tool_calls"] == 1
    assert observed["messages"][1][0]["content"] == "What is the current trend of EURUSD?"
    assert observed["messages"][1][1]["content"] == "EURUSD is bullish."
    assert observed["messages"][1][-1]["content"] == "Can I enter long for this trend?"


@pytest.mark.parametrize(
    ("prompt", "expected_tool", "expected_text"),
    [
        ("what is the H1 bias for EURUSD?", "get_market_bias", "EURUSD bias"),
        ("scan EURUSD for setups", "scan_setups", "EURUSD setup"),
        ("show me my watchlist", "get_watchlist", "XAUUSD, EURUSD"),
        (
            "scan XAUUSD, EURUSD and GBPUSD and find which one has the highest score",
            "rank_watchlist_pairs",
            "highest score",
        ),
    ],
)
def test_agent_routes_natural_language_queries_even_with_verbose_history(
    tmp_path,
    monkeypatch,
    prompt,
    expected_tool,
    expected_text,
) -> None:
    service, state = _agent_service(tmp_path)
    _seed_verbose_history(state)
    observed = {"messages": [], "tool_calls": []}

    class RoutingAgent:
        def __init__(self, tools):
            self.tools = {tool.name: tool for tool in tools}

        def stream(self, payload, config=None, stream_mode=None):
            messages = payload["messages"]
            observed["messages"].append(messages)
            assistant_messages = [item["content"] for item in messages if item["role"] == "assistant"]
            if any("Tool results:" in item for item in assistant_messages) or any(len(item) > 260 for item in assistant_messages):
                yield ("updates", {"model": {"messages": [AIMessage(content="No completed tool results were available.")]}})
                return

            last_message = messages[-1]["content"].lower()
            if "watchlist" in last_message:
                tool_name = "get_watchlist"
                tool_args = {}
                final = "XAUUSD, EURUSD and GBPUSD are on the watchlist."
            elif "highest score" in last_message:
                tool_name = "rank_watchlist_pairs"
                tool_args = {}
                final = "XAUUSD has the highest score on the current watchlist."
            elif "scan" in last_message:
                tool_name = "scan_setups"
                tool_args = {"pair": "EURUSD"}
                final = "EURUSD setup score is 8/10 long."
            else:
                tool_name = "get_market_bias"
                tool_args = {"pair": "EURUSD"}
                final = "EURUSD bias is bullish on H1."

            observed["tool_calls"].append(tool_name)
            yield (
                "updates",
                {
                    "model": {
                        "messages": [
                            AIMessage(
                                content="",
                                tool_calls=[{"name": tool_name, "args": tool_args, "id": "call-1", "type": "tool_call"}],
                            )
                        ]
                    }
                },
            )
            result = self.tools[tool_name].invoke(tool_args)
            yield ("updates", {"tools": {"messages": [ToolMessage(content=result, tool_call_id="call-1")]}})
            yield ("updates", {"model": {"messages": [AIMessage(content=final)]}})

    monkeypatch.setattr(
        "hedge_fund.chat.agent_runtime.AgentModelFactory.candidates",
        lambda self: [type("C", (), {"provider": "openai", "model_name": "gpt-5-mini", "model": object()})()],
    )
    monkeypatch.setattr("hedge_fund.chat.agent_runtime.create_agent", lambda model, tools, system_prompt: RoutingAgent(tools))

    response = service.process_message(state, prompt)

    assert observed["tool_calls"] == [expected_tool]
    assert expected_text.lower() in response.message.lower()
    assert "No completed tool results were available." not in response.message
    assistant_messages = [item["content"] for item in observed["messages"][0] if item["role"] == "assistant"]
    assert assistant_messages
    assert all("Tool results:" not in item for item in assistant_messages)
    assert all(len(item) <= 240 for item in assistant_messages)


def test_agent_tool_can_rank_watchlist_pairs(tmp_path) -> None:
    store, state = _state(tmp_path)
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session(state.session.session_id)
    artifacts = AgentArtifacts()
    context = AgentToolContext(
        settings=Settings.load(),
        state=state,
        scan_service=FakeScanService(),
        risk_service=FakeRiskService(),
        reverse_risk_service=ReverseRiskService(_MarketData(), _Broker()),
        config_manager=_config_manager(tmp_path),
        search_client=FakeSearchClient(),
        scratchpad=scratchpad,
        artifacts=artifacts,
        memory_repository=FakeMemoryRepository(),
        calendar_service=FakeCalendarService(),
    )

    tool = next(item for item in context.build_tools() if item.name == "rank_watchlist_pairs")
    payload = json.loads(tool.invoke({}))

    assert payload["ok"] is True
    assert payload["ranking"][0]["pair"] == "XAUUSD"
    assert artifacts.metadata["ranking"]


def test_agent_tool_get_watchlist_alias_returns_pairs(tmp_path) -> None:
    store, state = _state(tmp_path)
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session(state.session.session_id)
    artifacts = AgentArtifacts()
    context = AgentToolContext(
        settings=Settings.load(),
        state=state,
        scan_service=FakeScanService(),
        risk_service=FakeRiskService(),
        reverse_risk_service=ReverseRiskService(_MarketData(), _Broker()),
        config_manager=_config_manager(tmp_path),
        search_client=FakeSearchClient(),
        scratchpad=scratchpad,
        artifacts=artifacts,
        memory_repository=FakeMemoryRepository(),
        calendar_service=FakeCalendarService(),
    )

    tool = next(item for item in context.build_tools() if item.name == "get_watchlist")
    payload = json.loads(tool.invoke({}))

    assert payload["ok"] is True
    assert payload["pairs"][:3] == ["XAUUSD", "EURUSD", "GBPUSD"]


def test_agent_tool_ranking_skips_pairs_without_scan_results(tmp_path) -> None:
    class SparseScanService(FakeScanService):
        def scan(self, pairs):
            if pairs[0] == "EURUSD":
                return ScanResultBundle(biases=[], setups=[], ai_analysis=[])
            return super().scan(pairs)

    store, state = _state(tmp_path)
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session(state.session.session_id)
    artifacts = AgentArtifacts()
    context = AgentToolContext(
        settings=Settings.load(),
        state=state,
        scan_service=SparseScanService(),
        risk_service=FakeRiskService(),
        reverse_risk_service=ReverseRiskService(_MarketData(), _Broker()),
        config_manager=_config_manager(tmp_path),
        search_client=FakeSearchClient(),
        scratchpad=scratchpad,
        artifacts=artifacts,
        memory_repository=FakeMemoryRepository(),
        calendar_service=FakeCalendarService(),
    )

    tool = next(item for item in context.build_tools() if item.name == "rank_watchlist_pairs")
    payload = json.loads(tool.invoke({}))

    assert payload["ok"] is True
    assert all(item["pair"] != "EURUSD" for item in payload["ranking"])


def test_agent_tool_can_access_memory_and_calendar(tmp_path) -> None:
    store, state = _state(tmp_path)
    scratchpad = ScratchpadManager(tmp_path, Settings.load().agent).for_session(state.session.session_id)
    artifacts = AgentArtifacts()
    context = AgentToolContext(
        settings=Settings.load(),
        state=state,
        scan_service=FakeScanService(),
        risk_service=FakeRiskService(),
        reverse_risk_service=ReverseRiskService(_MarketData(), _Broker()),
        config_manager=_config_manager(tmp_path),
        search_client=FakeSearchClient(),
        scratchpad=scratchpad,
        artifacts=artifacts,
        memory_repository=FakeMemoryRepository(),
        calendar_service=FakeCalendarService(),
    )

    memory_tool = next(item for item in context.build_tools() if item.name == "show_memory")
    calendar_tool = next(item for item in context.build_tools() if item.name == "get_economic_calendar")

    memory_payload = json.loads(memory_tool.invoke({}))
    calendar_payload = json.loads(calendar_tool.invoke({"view": "today"}))

    assert "BOE" in memory_payload["content"]
    assert calendar_payload["calendar"]["events"][0]["event_name"] == "CPI"
