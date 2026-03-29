from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage

from hedge_fund.chat.agent_models import AgentModelFactory
from hedge_fund.chat.scratchpad import ScratchpadLogger
from hedge_fund.config.environment import EnvironmentSettings
from hedge_fund.config.settings import Settings
from hedge_fund.domain.exceptions import ProviderError
from hedge_fund.domain.models import AiAnalysisResult, BiasResult, RiskCalculation, SetupScanResult, TradePlanOutput

try:
    from langgraph.errors import GraphRecursionError
except Exception:  # noqa: BLE001
    class GraphRecursionError(Exception):
        """Fallback recursion error when langgraph is unavailable."""


class AgentEventSink(Protocol):
    def update_status(self, message: str) -> None: ...

    def emit_plan(self, message: str) -> None: ...

    def emit_reasoning(self, message: str) -> None: ...


@dataclass
class AgentArtifacts:
    biases: list[BiasResult] = field(default_factory=list)
    setups: list[SetupScanResult] = field(default_factory=list)
    ai_analysis: list[AiAnalysisResult] = field(default_factory=list)
    risk: RiskCalculation | None = None
    reverse_risk: Any = None
    trade_plan: TradePlanOutput | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    summaries: list[str] = field(default_factory=list)


@dataclass
class AgentRunResult:
    message: str
    artifacts: AgentArtifacts
    metadata: dict[str, Any]


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        env: EnvironmentSettings,
        logger: logging.Logger,
        model_override: str | None = None,
    ) -> None:
        self.settings = settings
        self.env = env
        self.logger = logger
        self.model_override = model_override

    def run(
        self,
        user_message: str,
        system_prompt: str,
        tools: list[Any],
        scratchpad: ScratchpadLogger,
        artifacts: AgentArtifacts,
        event_sink: AgentEventSink | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        stream_handler: Callable[[str], None] | None = None,
        prophet_md: str = "",
        plan_handler: Callable[[str, list[dict[str, Any]]], str | None] | None = None,
        reasoning_handler: Callable[[str, str, dict[str, Any]], str] | None = None,
    ) -> AgentRunResult:
        failures: list[str] = []
        plan = self._build_plan(user_message, history_messages or [], plan_handler)
        scratchpad.ensure_init(user_message, plan)
        if plan and event_sink:
            event_sink.emit_plan(plan)
        try:
            candidates = AgentModelFactory(self.settings, self.env, self.model_override).candidates()
        except ProviderError as exc:
            failures.append(str(exc))
            candidates = []

        for candidate in candidates:
            scratchpad.log_thinking(
                f"Starting provider {candidate.provider} with model {candidate.model_name}.",
                {
                    "event": "provider_start",
                    "provider": candidate.provider,
                    "model": candidate.model_name,
                },
            )
            try:
                return self._run_with_candidate(
                    user_message=user_message,
                    system_prompt=system_prompt,
                    tools=tools,
                    scratchpad=scratchpad,
                    artifacts=artifacts,
                    candidate=candidate,
                    event_sink=event_sink,
                    history_messages=history_messages,
                    stream_handler=stream_handler,
                    prophet_md=prophet_md,
                    reasoning_handler=reasoning_handler,
                )
            except GraphRecursionError:
                self.logger.warning("Agent recursion limit reached for session")
                partial = self._partial_message(artifacts, "I hit the configured reasoning-step limit, so this is a partial result.")
                scratchpad.log_thinking(
                    "The agent hit the configured reasoning-step limit and returned a partial result.",
                    {
                        "provider": candidate.provider,
                        "model": candidate.model_name,
                        "partial": True,
                    },
                )
                return AgentRunResult(
                    message=partial,
                    artifacts=artifacts,
                    metadata={"provider": candidate.provider, "model": candidate.model_name, "partial": True},
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{candidate.provider}: {exc}")
                scratchpad.log_thinking(
                    f"Provider {candidate.provider} failed and the runtime is falling back.",
                    {
                        "event": "provider_failure",
                        "provider": candidate.provider,
                        "model": candidate.model_name,
                        "error": str(exc),
                    },
                )
                self.logger.warning("Agent provider %s failed: %s", candidate.provider, exc)

        fallback = self._partial_message(artifacts, "The agent fell back after provider failures.")
        scratchpad.log_thinking(
            "All configured providers failed, so the runtime returned the best partial result available.",
            {"partial": True, "failures": failures},
        )
        return AgentRunResult(
            message=fallback,
            artifacts=artifacts,
            metadata={"provider_failures": failures, "partial": True},
        )

    def _run_with_candidate(
        self,
        user_message: str,
        system_prompt: str,
        tools: list[Any],
        scratchpad: ScratchpadLogger,
        artifacts: AgentArtifacts,
        candidate,
        event_sink: AgentEventSink | None,
        history_messages: list[dict[str, Any]] | None,
        stream_handler: Callable[[str], None] | None,
        prophet_md: str,
        reasoning_handler: Callable[[str, str, dict[str, Any]], str] | None,
    ) -> AgentRunResult:
        agent = create_agent(
            candidate.model,
            tools=tools,
            system_prompt=system_prompt,
        )
        final_message = ""
        streamed_parts: list[str] = []
        render_state = {
            "suppress_model_stream": False,
            "trade_plan_message": "",
            "trade_plan_emitted": False,
        }
        if event_sink:
            event_sink.update_status("Thinking...")
        pending_tool_calls: dict[str, dict[str, Any]] = {}

        stream = agent.stream(
            {"messages": history_messages or [{"role": "user", "content": user_message}]},
            config={"recursion_limit": max(10, self.settings.agent.max_steps * 4)},
            stream_mode=["messages", "updates"],
        )
        for stream_mode, payload in stream:
            if stream_mode == "messages":
                if render_state["suppress_model_stream"]:
                    continue
                text = self._stream_text(payload)
                if text:
                    streamed_parts.append(text)
                    if stream_handler:
                        stream_handler(text)
                continue
            if stream_mode != "updates":
                continue
            for update in payload.values():
                message = self._latest_message(update)
                if message is None:
                    continue
                self._handle_message(
                    message,
                    scratchpad,
                    candidate.provider,
                    candidate.model_name,
                    event_sink,
                    pending_tool_calls,
                    reasoning_handler,
                    user_message,
                    artifacts,
                    stream_handler,
                    render_state,
                )
                if isinstance(message, AIMessage) and not message.tool_calls and not render_state["trade_plan_message"]:
                    final_message = self._coerce_text(message)

        if render_state["trade_plan_message"]:
            final_message = render_state["trade_plan_message"]
        if not final_message:
            if streamed_parts:
                final_message = "".join(streamed_parts).strip()
        if not final_message:
            final_message = self._partial_message(artifacts, "I could not complete a final answer, so here is the latest partial result.")
        final_message = self._apply_validation_flags(final_message, prophet_md, artifacts, scratchpad)
        return AgentRunResult(
            message=final_message,
            artifacts=artifacts,
            metadata={"provider": candidate.provider, "model": candidate.model_name},
        )

    def _handle_message(
        self,
        message: BaseMessage,
        scratchpad: ScratchpadLogger,
        provider: str,
        model_name: str,
        event_sink: AgentEventSink | None,
        pending_tool_calls: dict[str, dict[str, Any]],
        reasoning_handler: Callable[[str, str, dict[str, Any]], str] | None,
        user_message: str,
        artifacts: AgentArtifacts,
        stream_handler: Callable[[str], None] | None,
        render_state: dict[str, Any],
    ) -> None:
        if isinstance(message, AIMessage) and message.tool_calls:
            for tool_call in message.tool_calls:
                name = tool_call.get("name", "unknown")
                arguments = tool_call.get("args", {})
                call_id = str(tool_call.get("id", ""))
                if call_id:
                    pending_tool_calls[call_id] = {"name": name, "arguments": arguments}
                scratchpad.log_thinking(
                    f"Selected tool {name} for the next check.",
                    {
                        "event": "tool_selected",
                        "provider": provider,
                        "model": model_name,
                        "tool": name,
                        "arguments": arguments,
                    },
                )
                if event_sink:
                    event_sink.update_status(self._status_for_tool(name))
                    observation = self._reasoning_message(
                        name,
                        "before",
                        {"tool": name, **arguments},
                        reasoning_handler,
                        user_message,
                        artifacts,
                    )
                    event_sink.emit_reasoning(observation)
            return

        if isinstance(message, ToolMessage):
            tool_info = pending_tool_calls.pop(message.tool_call_id, {"name": "unknown", "arguments": {}})
            payload = self._parse_tool_message(message)
            scratchpad.log_thinking(
                f"Received a result from {tool_info['name']}.",
                {
                    "event": "tool_message",
                    "provider": provider,
                    "model": model_name,
                    "tool": tool_info["name"],
                    "content": self._coerce_text(message),
                    "payload": payload,
                },
            )
            if event_sink and self._should_emit_tool_reasoning(tool_info["name"], payload):
                observation = self._reasoning_message(
                    tool_info["name"],
                    "after",
                    payload,
                    reasoning_handler,
                    user_message,
                    artifacts,
                )
                event_sink.emit_reasoning(observation)
            self._capture_trade_plan(tool_info["name"], payload, artifacts, stream_handler, render_state)
            return

        if isinstance(message, AIMessage):
            text = self._coerce_text(message)
            if text:
                scratchpad.log_thinking(
                    "The model is assembling the final trader-facing response.",
                    {
                        "event": "finalizing",
                        "provider": provider,
                        "model": model_name,
                        "content": text,
                    },
                )
                if event_sink:
                    event_sink.update_status("Propheting...")
                    event_sink.emit_reasoning("Building the final analysis from the strongest signals.")

    def _should_emit_tool_reasoning(self, tool_name: str, payload: dict[str, Any]) -> bool:
        if tool_name == "generate_trade_plan" and payload.get("ok") is False:
            return False
        return True

    def _coerce_text(self, message: BaseMessage) -> str:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content.strip()
        parts: list[str] = []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
        return "\n".join(part for part in parts if part).strip()

    def _latest_message(self, update: Any) -> BaseMessage | None:
        if not isinstance(update, dict):
            return None
        messages = update.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        message = messages[-1]
        return message if isinstance(message, BaseMessage) else None

    def _stream_text(self, payload: Any) -> str:
        if not isinstance(payload, tuple) or not payload:
            return ""
        message = payload[0]
        if not isinstance(message, BaseMessage):
            return ""
        if getattr(message, "tool_calls", None):
            return ""
        if isinstance(message, ToolMessage):
            return ""
        if not isinstance(message, (AIMessage, AIMessageChunk)):
            return ""
        text = self._coerce_text(message)
        if not text:
            return ""
        stripped = text.lstrip()
        if self._looks_like_tool_payload(stripped):
            return ""
        if not any(character.isalpha() for character in stripped):
            return ""
        return text

    def _parse_tool_message(self, message: ToolMessage) -> dict[str, Any]:
        text = self._coerce_text(message)
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"content": text}
        return parsed if isinstance(parsed, dict) else {"content": parsed}

    def _reasoning_message(
        self,
        tool_name: str,
        phase: str,
        payload: dict[str, Any],
        reasoning_handler: Callable[[str, str, dict[str, Any]], str] | None,
        user_message: str,
        artifacts: AgentArtifacts,
    ) -> str:
        if reasoning_handler is not None:
            try:
                message = reasoning_handler(tool_name, phase, payload)
                if message and message.strip():
                    return message.strip()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Reasoning handler failed for %s (%s): %s", tool_name, phase, exc)
        summary = str(payload.get("summary") or payload.get("recommendation") or "").strip()
        if phase == "before":
            pair = payload.get("pair")
            query = payload.get("query")
            if pair:
                return f"Checking {pair} next so I can tighten the read."
            if query:
                return f"Searching for {query} to pull in current context."
            return f"Running {tool_name.replace('_', ' ')} next."
        if summary:
            return summary if summary.endswith((".", "!", "?")) else f"{summary}."
        if payload.get("ok") is False:
            error = str(payload.get("error") or "the tool returned an issue").strip()
            return f"I hit an issue there: {error}."
        recent = artifacts.summaries[-1] if artifacts.summaries else user_message
        return recent if recent.endswith((".", "!", "?")) else f"{recent}."

    def _looks_like_tool_payload(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if stripped[0] not in "[{":
            return any(token in stripped for token in ('"tool_call"', '"tool_calls"', '"arguments"', '"result"'))
        try:
            payload = json.loads(stripped)
        except ValueError:
            return False
        return self._contains_tool_payload(payload)

    def _contains_tool_payload(self, payload: Any) -> bool:
        if isinstance(payload, dict):
            keys = {str(key).lower() for key in payload}
            if {"tool_call", "tool_calls"} & keys:
                return True
            if {"arguments", "args"} & keys and {"name", "tool"} & keys:
                return True
            if "tool" in keys and ("ok" in keys or "result" in keys):
                return True
            payload_type = str(payload.get("type", "")).lower()
            if payload_type in {"tool_use", "tool_result", "function"}:
                return True
            return any(self._contains_tool_payload(value) for value in payload.values())
        if isinstance(payload, list):
            return any(self._contains_tool_payload(item) for item in payload)
        return False

    def _partial_message(self, artifacts: AgentArtifacts, note: str) -> str:
        lines = list(artifacts.summaries[-3:])
        if not lines:
            lines.append("No completed tool results were available.")
        lines.append(note)
        return "\n".join(lines)

    def _status_for_tool(self, tool_name: str) -> str:
        mapping = {
            "get_market_bias": "Reading market structure...",
            "scan_setups": "Scanning for confluence...",
            "calculate_risk": "Calculating position size...",
            "calculate_risk_exposure": "Calculating position size...",
            "generate_trade_plan": "Building trade plan...",
            "get_session_status": "Checking session...",
            "get_economic_calendar": "Checking the calendar...",
            "rank_watchlist_pairs": "Ranking watchlist setups...",
            "get_watchlist": "Checking watchlist...",
            "show_watchlist": "Checking watchlist...",
            "show_memory": "Loading trader memory...",
            "remember_rule": "Updating trader memory...",
            "forget_rule": "Updating trader memory...",
            "web_search": "Searching the web...",
        }
        return mapping.get(tool_name, "Analysing...")

    def _capture_trade_plan(
        self,
        tool_name: str,
        payload: dict[str, Any],
        artifacts: AgentArtifacts,
        stream_handler: Callable[[str], None] | None,
        render_state: dict[str, Any],
    ) -> None:
        if tool_name != "generate_trade_plan":
            return
        trade_plan_payload = payload.get("trade_plan")
        if not isinstance(trade_plan_payload, dict):
            return
        trade_plan = TradePlanOutput.model_validate(trade_plan_payload)
        artifacts.trade_plan = trade_plan
        artifacts.metadata["trade_plan"] = trade_plan.model_dump(mode="json")
        combined = f"{trade_plan.narrative}\n\n{trade_plan.formatted_block}"
        render_state["trade_plan_message"] = combined
        render_state["suppress_model_stream"] = True
        if render_state["trade_plan_emitted"] or stream_handler is None:
            return
        for chunk in re.findall(r"\S+\s*", trade_plan.narrative):
            stream_handler(chunk)
        stream_handler("\n\n")
        stream_handler(trade_plan.formatted_block)
        render_state["trade_plan_emitted"] = True

    def _build_plan(
        self,
        user_message: str,
        history_messages: list[dict[str, Any]],
        plan_handler: Callable[[str, list[dict[str, Any]]], str | None] | None,
    ) -> str | None:
        if plan_handler is None:
            return None
        try:
            plan = plan_handler(user_message, history_messages)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Planning handler failed: %s", exc)
            return None
        return plan.strip() if isinstance(plan, str) and plan.strip() else None

    def _apply_validation_flags(
        self,
        message: str,
        prophet_md: str,
        artifacts: AgentArtifacts,
        scratchpad: ScratchpadLogger,
    ) -> str:
        if not prophet_md.strip():
            return message
        flags = self._validation_flags(artifacts)
        if not flags:
            return message
        scratchpad.log_thinking(
            "The runtime appended validation flags from PROPHET guardrails to the final response.",
            {"flags": flags},
        )
        return f"{message.rstrip()}\n\nValidation Flags:\n" + "\n".join(f"- {flag}" for flag in flags)

    def _validation_flags(self, artifacts: AgentArtifacts) -> list[str]:
        flags: list[str] = []
        current_session = self._current_session_name()
        trade_plan_session = (artifacts.trade_plan.session if artifacts.trade_plan else "").strip()
        if current_session == "Asia" or trade_plan_session.lower() == "asia":
            flags.append("Asia session trading is active, which violates the no-Asia-session guardrail.")

        news_flag = self._news_validation_flag(artifacts)
        if news_flag:
            flags.append(news_flag)

        if self._has_ranging_bias(artifacts):
            flags.append("Market structure is ranging with no clear directional bias, so stand-aside conditions apply.")

        setup_count = max(len(artifacts.setups), len(artifacts.metadata.get("ranking") or []))
        if setup_count > 3:
            flags.append(f"More than 3 setups were surfaced in one session ({setup_count}), so focus needs tightening.")

        return flags

    def _current_session_name(self) -> str:
        from hedge_fund.chat.utils import current_session_status

        return str(current_session_status(self.settings.trading.sessions).get("current_session") or "").strip()

    def _news_validation_flag(self, artifacts: AgentArtifacts) -> str | None:
        calendar = artifacts.metadata.get("calendar")
        if not isinstance(calendar, dict):
            return None
        pair = self._validation_pair(artifacts)
        if not pair:
            return None
        now = datetime.now(tz=UTC)
        for event in calendar.get("events") or []:
            if not isinstance(event, dict):
                continue
            if str(event.get("impact", "")).lower() != "high":
                continue
            if not self._event_affects_pair(str(event.get("currency", "")), pair):
                continue
            scheduled = self._parse_event_time(str(event.get("date", "")), str(event.get("time_utc", "")))
            if scheduled is None:
                continue
            if abs(now - scheduled) <= timedelta(minutes=15):
                event_name = str(event.get("event_name") or "high-impact event").strip()
                return f"High-impact news ({event_name}) is within 15 minutes for {pair}, so entries should be avoided."
        return None

    def _validation_pair(self, artifacts: AgentArtifacts) -> str | None:
        if artifacts.trade_plan is not None:
            return artifacts.trade_plan.pair
        if artifacts.biases:
            return artifacts.biases[0].pair
        if artifacts.setups:
            return artifacts.setups[0].pair
        ranking = artifacts.metadata.get("ranking") or []
        if ranking and isinstance(ranking[0], dict):
            return str(ranking[0].get("pair") or "").strip() or None
        return None

    def _parse_event_time(self, date_value: str, time_value: str) -> datetime | None:
        if not date_value or not time_value:
            return None
        try:
            return datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        except ValueError:
            return None

    def _event_affects_pair(self, currency: str, pair: str) -> bool:
        if pair == "XAUUSD":
            return currency == "USD"
        return currency in pair

    def _has_ranging_bias(self, artifacts: AgentArtifacts) -> bool:
        if any(item.bias == "Ranging" for item in artifacts.biases):
            return True
        if artifacts.setups and all(item.direction == "Neutral" for item in artifacts.setups):
            return True
        return False
