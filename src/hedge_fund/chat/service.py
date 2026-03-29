from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import re
from types import SimpleNamespace

from hedge_fund.chat.agent_runtime import AgentArtifacts, AgentEventSink, AgentRuntime
from hedge_fund.chat.agent_tools import AgentToolContext
from hedge_fund.chat.ai import ChatLanguageService
from hedge_fund.chat.config_manager import ConfigManager
from hedge_fund.chat.models import ChatContextSnapshot, ChatResponse, ChatSessionState, ChatTurn, ReverseRiskCalculation, RouteDecision
from hedge_fund.chat.scratchpad import ScratchpadManager
from hedge_fund.chat.session_store import SessionStore
from hedge_fund.chat.utils import current_session_status, normalize_model_override, normalize_pair_alias, pip_value_per_standard_lot
from hedge_fund.config.settings import Settings
from hedge_fund.services.calendar_service import CalendarService
from hedge_fund.services.communication_styles import get_style_modifier
from hedge_fund.services.prophet_md_generator import generate_prophet_md
from hedge_fund.services.scan_service import RiskService, ScanService
from hedge_fund.services.skill_detector import detect_skill_signals

SINGLE_CHART_ANALYSIS_PROMPT = (
    "You are analysing a trading chart image provided by the user.\n"
    "Examine the chart carefully before responding.\n\n"
    "Respond in this order:\n"
    "1. What Prophet sees - brief description of the chart\n"
    "2. Market structure - trend direction and swing points\n"
    "3. Key levels identified - support/resistance, FVGs, and Fib zones\n"
    "4. Bias - bullish, bearish, or neutral with reasoning\n"
    "5. Go/No-Go - based on the user's PROPHET.md rules\n"
    "6. What to watch - the single most important level or signal\n\n"
    "Look for and identify:\n"
    "- Market structure: trend direction, swing highs and lows (HH/HL or LH/LL)\n"
    "- Key support and resistance levels visible on the chart\n"
    "- Fair Value Gaps (FVGs): imbalance zones between non-overlapping candles\n"
    "- Fibonacci zones if a clear swing is visible (focus on 0.5 to 0.786)\n"
    "- Liquidity areas: equal highs, equal lows, and session extremes\n"
    "- Current price position relative to all identified levels\n"
    "- Timeframe if visible in the chart\n"
    "- Visible confluence between the signals on the chart\n\n"
    "Cross-reference your observations against the user's PROPHET.md rules.\n"
    "Conclude with: bias direction, key level to watch, and a go/no-go verdict.\n\n"
    "User's question about the chart:\n"
    "{user_message}"
)

MULTI_TIMEFRAME_ANALYSIS_PROMPT = (
    "You have been provided with two trading chart images for multi-timeframe analysis.\n"
    "Analyse both charts carefully before responding.\n\n"
    "Respond in this order:\n"
    "1. What Prophet sees - brief description of both charts\n"
    "2. Market structure - structure and swing points for each chart\n"
    "3. Key levels identified - support/resistance, FVGs, and Fib zones on both charts\n"
    "4. Bias - combined bullish, bearish, or neutral read with reasoning\n"
    "5. Go/No-Go - based on alignment with the user's PROPHET.md rules\n"
    "6. What to watch - the single most important shared level or signal\n\n"
    "Identify which timeframe each chart shows if visible.\n"
    "For each chart, identify structure, key levels, FVGs, liquidity, and bias.\n"
    "Then assess:\n"
    "- Do the two timeframes align in bias?\n"
    "- Is there confluence between the levels on both timeframes?\n"
    "- Does the higher timeframe structure support the lower timeframe entry?\n\n"
    "Cross-reference all observations against the user's PROPHET.md rules.\n"
    "Conclude with: combined bias, confluence verdict, and go/no-go.\n\n"
    "User's question about the charts:\n"
    "{user_message}"
)


class ReverseRiskService:
    def __init__(self, market_data, broker) -> None:
        self.market_data = market_data
        self.broker = broker

    def calculate(self, pair: str, lot_size: float, sl_pips: int) -> ReverseRiskCalculation:
        balance = self.broker.get_account_balance()
        price = self.market_data.get_price(pair)
        metadata = {} if pair == "XAUUSD" else self.broker.get_instrument_metadata(pair)
        pip_value, pip_size = pip_value_per_standard_lot(pair, price, metadata)
        risk_amount = lot_size * sl_pips * pip_value
        risk_pct = (risk_amount / balance) * 100 if balance else 0.0
        return ReverseRiskCalculation(
            pair=pair,
            account_balance=round(balance, 2),
            lot_size=lot_size,
            sl_pips=sl_pips,
            risk_amount=round(risk_amount, 2),
            risk_pct=round(risk_pct, 2),
            current_price=round(price, 5),
            pip_value_per_standard_lot=round(pip_value, 5),
            stop_distance=round(sl_pips * pip_size, 5),
        )


class ChatService:
    def __init__(
        self,
        settings: Settings,
        scan_service: ScanService,
        risk_service: RiskService,
        reverse_risk_service: ReverseRiskService,
        language: ChatLanguageService,
        config_manager: ConfigManager,
        session_store: SessionStore,
        agent_runtime: AgentRuntime | None = None,
        scratchpad_manager: ScratchpadManager | None = None,
        search_client=None,
        memory_repository=None,
        user_profile_repository=None,
        calendar_service: CalendarService | None = None,
        device_token: str | None = None,
    ) -> None:
        self.settings = settings
        self.scan_service = scan_service
        self.risk_service = risk_service
        self.reverse_risk_service = reverse_risk_service
        self.language = language
        self.config_manager = config_manager
        self.session_store = session_store
        self.agent_runtime = agent_runtime
        self.scratchpad_manager = scratchpad_manager
        self.search_client = search_client
        self.memory_repository = memory_repository
        self.user_profile_repository = user_profile_repository
        self.calendar_service = calendar_service
        self.device_token = (device_token or "").strip() or None

    def process_message(
        self,
        state: ChatSessionState,
        message: str,
        authorize_mutation: Callable[[str], bool] | None = None,
        event_sink: AgentEventSink | None = None,
        stream_handler: Callable[[str], None] | None = None,
        image_attachments: list[dict[str, str]] | None = None,
    ) -> ChatResponse:
        original_content = message.strip()
        content = original_content
        if not content:
            return ChatResponse(session_id=state.session.session_id, message="Enter a request or use /help.")

        confirmation_prefix = ""
        pending_confirmation = state.session.context.style_suggestion_pending
        if pending_confirmation:
            confirmation_prefix, content = self._resolve_style_confirmation(state, content)
            if not content:
                response = ChatResponse(session_id=state.session.session_id, message=confirmation_prefix.strip())
                return self._record(state, original_content, response)

        if content.startswith("/"):
            response = self._handle_slash_command(state, content, authorize_mutation)
            response = self._with_style_prefix(response, confirmation_prefix)
            return self._record(state, original_content, response)

        fast_route = self._match_fast_config_command(content)
        if fast_route:
            response = self._handle_config_mutation(state, fast_route, authorize_mutation)
            response = self._with_style_prefix(response, confirmation_prefix)
            return self._record(state, original_content, response)

        if self.agent_runtime and self.scratchpad_manager:
            response = self._handle_agent_message(state, content, authorize_mutation, event_sink, stream_handler, image_attachments)
            response = self._finalize_adaptive_style_response(state, original_content, response, confirmation_prefix, pending_confirmation)
            return self._record(state, original_content, response)

        route = self.language.route(content, self._routing_context(state))
        if route.intent == "unknown":
            response = ChatResponse(
                session_id=state.session.session_id,
                route=route,
                message="I couldn’t pin that down. Ask for bias, setups, risk, sessions, or config changes.",
            )
            response = self._finalize_adaptive_style_response(state, original_content, response, confirmation_prefix, pending_confirmation)
            return self._record(state, original_content, response)

        if route.missing_fields:
            response = ChatResponse(
                session_id=state.session.session_id,
                route=route,
                message=self._missing_fields_message(route),
            )
            response = self._finalize_adaptive_style_response(state, original_content, response, confirmation_prefix, pending_confirmation)
            return self._record(state, original_content, response)

        if route.intent == "bias":
            response = self._handle_bias(state, route)
        elif route.intent == "scan":
            response = self._handle_scan(state, route)
        elif route.intent == "risk_size":
            response = self._handle_risk_size(state, route)
        elif route.intent == "risk_exposure":
            response = self._handle_risk_exposure(state, route)
        elif route.intent == "config_show_pairs":
            response = ChatResponse(
                session_id=state.session.session_id,
                route=route,
                message="Watching: " + ", ".join(self.config_manager.show_pairs()),
                metadata={"pairs": self.config_manager.show_pairs()},
            )
        elif route.intent == "config_show_risk":
            risk = self.config_manager.show_risk()
            response = ChatResponse(
                session_id=state.session.session_id,
                route=route,
                message=(
                    f"Default risk is {risk['default_risk_pct']}%, minimum RR is {risk['minimum_rr']}, "
                    f"preferred RR is {risk['preferred_rr']}."
                ),
                metadata=risk,
            )
        elif route.intent in {"config_add_pair", "config_remove_pair"}:
            response = self._handle_config_mutation(state, route, authorize_mutation)
        elif route.intent == "session_status":
            session_state = current_session_status(self.settings.trading.sessions)
            response = ChatResponse(
                session_id=state.session.session_id,
                route=route,
                message=session_state["status"],
                metadata=session_state,
            )
        else:
            answer = self.language.answer_general(content, self._answer_context(state, route))
            response = ChatResponse(
                session_id=state.session.session_id,
                route=route,
                message=answer,
            )
        response = self._finalize_adaptive_style_response(state, original_content, response, confirmation_prefix, pending_confirmation)
        return self._record(state, original_content, response)

    def _handle_agent_message(
        self,
        state: ChatSessionState,
        content: str,
        authorize_mutation: Callable[[str], bool] | None,
        event_sink: AgentEventSink | None,
        stream_handler: Callable[[str], None] | None,
        image_attachments: list[dict[str, str]] | None,
    ) -> ChatResponse:
        scratchpad = self.scratchpad_manager.for_session(state.session.session_id)
        artifacts = AgentArtifacts()
        prophet_md = self._current_memory_content().strip()
        tool_context = AgentToolContext(
            settings=self.settings,
            state=state,
            scan_service=self.scan_service,
            risk_service=self.risk_service,
            reverse_risk_service=self.reverse_risk_service,
            config_manager=self.config_manager,
            search_client=self.search_client,
            memory_repository=self.memory_repository,
            calendar_service=self.calendar_service,
            scratchpad=scratchpad,
            artifacts=artifacts,
            authorize_mutation=authorize_mutation,
            tool_summary_handler=(
                None
                if not hasattr(self.language, "summarize_tool_result")
                else lambda tool_name, arguments, raw_result: self.language.summarize_tool_result(
                    tool_name,
                    arguments,
                    raw_result,
                    user_message=content,
                )
            ),
        )
        self.agent_runtime.model_override = normalize_model_override(state.session.model_override)
        summarize_tool_reasoning = getattr(self.language, "summarize_tool_reasoning", None)
        plan_agent_run = getattr(self.language, "plan_agent_run", None)
        result = self.agent_runtime.run(
            user_message=content,
            system_prompt=self._agent_system_prompt(state),
            tools=tool_context.build_tools(),
            scratchpad=scratchpad,
            artifacts=artifacts,
            event_sink=event_sink,
            history_messages=self._agent_messages(state, content, image_attachments=image_attachments),
            stream_handler=stream_handler,
            prophet_md=prophet_md,
            plan_handler=(
                None
                if not callable(plan_agent_run)
                else lambda user_message, history_messages: plan_agent_run(user_message, history_messages)
            ),
            reasoning_handler=(
                None
                if not callable(summarize_tool_reasoning)
                else lambda tool_name, phase, payload: summarize_tool_reasoning(
                    tool_name,
                    phase,
                    payload,
                    user_message=content,
                    recent_summaries=list(artifacts.summaries[-3:]),
                )
            ),
        )
        self._refresh_settings(self.config_manager.current_settings())
        return ChatResponse(
            session_id=state.session.session_id,
            message=result.message,
            biases=artifacts.biases,
            setups=artifacts.setups,
            ai_analysis=artifacts.ai_analysis,
            risk=artifacts.risk,
            reverse_risk=artifacts.reverse_risk,
            metadata={**artifacts.metadata, **result.metadata, "tool_summaries": artifacts.summaries},
        )

    def _handle_bias(self, state: ChatSessionState, route: RouteDecision) -> ChatResponse:
        pairs = self._resolve_pairs(state, route)
        biases = self.scan_service.bias_only(pairs)
        state.session.context.active_pair = pairs[0] if len(pairs) == 1 else state.session.context.active_pair
        state.session.context.last_pairs = pairs
        state.session.context.last_bias_pairs = pairs
        state.session.context.last_intent = route.intent
        return ChatResponse(
            session_id=state.session.session_id,
            route=route,
            biases=biases,
            metadata={"pairs": pairs},
        )

    def _handle_scan(self, state: ChatSessionState, route: RouteDecision) -> ChatResponse:
        pairs = self._resolve_pairs(state, route)
        bundle = self.scan_service.scan(pairs)
        threshold = route.score_threshold
        if threshold is None and route.question and "high probability" in route.question.lower():
            threshold = max(self.settings.trading.scanner.minimum_score, 7)
        if threshold is not None:
            matching_pairs = {setup.pair for setup in bundle.setups if setup.score >= threshold}
            biases = [item for item in bundle.biases if item.pair in matching_pairs]
            setups = [item for item in bundle.setups if item.pair in matching_pairs]
            ai_analysis = [item for item in bundle.ai_analysis if any(pair in item.narrative for pair in matching_pairs)] or bundle.ai_analysis
        else:
            biases = bundle.biases
            setups = bundle.setups
            ai_analysis = bundle.ai_analysis

        if len(pairs) == 1:
            state.session.context.active_pair = pairs[0]
            state.session.context.last_scan_pair = pairs[0]
        state.session.context.last_pairs = pairs
        state.session.context.last_setup_pairs = [item.pair for item in setups]
        state.session.context.last_intent = route.intent
        return ChatResponse(
            session_id=state.session.session_id,
            route=route,
            biases=biases,
            setups=setups,
            ai_analysis=ai_analysis,
            metadata={"pairs": pairs, "score_threshold": threshold},
        )

    def _handle_risk_size(self, state: ChatSessionState, route: RouteDecision) -> ChatResponse:
        calculation = self.risk_service.calculate(route.pair or "", route.risk_pct or 0, route.sl_pips or 0)
        state.session.context.active_pair = calculation.pair
        state.session.context.last_intent = route.intent
        return ChatResponse(
            session_id=state.session.session_id,
            route=route,
            risk=calculation,
        )

    def _handle_risk_exposure(self, state: ChatSessionState, route: RouteDecision) -> ChatResponse:
        calculation = self.reverse_risk_service.calculate(route.pair or "", route.lot_size or 0, route.sl_pips or 0)
        state.session.context.active_pair = calculation.pair
        state.session.context.last_intent = route.intent
        return ChatResponse(
            session_id=state.session.session_id,
            route=route,
            reverse_risk=calculation,
        )

    def _handle_config_mutation(
        self,
        state: ChatSessionState,
        route: RouteDecision,
        authorize_mutation: Callable[[str], bool] | None,
    ) -> ChatResponse:
        permission_mode = state.session.permission_mode
        pair = route.pair or ""
        if permission_mode == "plan":
            return ChatResponse(
                session_id=state.session.session_id,
                route=route,
                message="Permission mode is plan, so config changes are blocked for this session.",
            )
        if permission_mode == "default":
            if authorize_mutation is None or not authorize_mutation(f"Update config.yaml for {pair}?"):
                return ChatResponse(
                    session_id=state.session.session_id,
                    route=route,
                    message="Config change cancelled.",
                )

        settings = self.config_manager.add_pair(pair) if route.intent == "config_add_pair" else self.config_manager.remove_pair(pair)
        self._refresh_settings(settings)
        action = "Added" if route.intent == "config_add_pair" else "Removed"
        state.session.context.last_intent = route.intent
        return ChatResponse(
            session_id=state.session.session_id,
            route=route,
            message=f"{action} {pair} in config.yaml.",
            metadata={"pairs": settings.trading.pairs},
        )

    def _handle_slash_command(
        self,
        state: ChatSessionState,
        command: str,
        authorize_mutation: Callable[[str], bool] | None,
    ) -> ChatResponse:
        parts = command.split()
        cmd = parts[0].lower()
        if cmd == "/help":
            return ChatResponse(
                session_id=state.session.session_id,
                message="Available session commands:",
                metadata={
                    "view": "help_menu",
                    "commands": [
                        ("/help", "List all available commands"),
                        ("/memory", "Show current PROPHET.md contents"),
                        ("/remember [rule]", "Add a rule to PROPHET.md"),
                        ("/forget [rule]", "Remove a rule from PROPHET.md"),
                        ("/model", "Select the active AI model for this session"),
                        ("/pairs", "View, add, or remove watchlist pairs"),
                        ("/sessions", "List and resume saved sessions"),
                        ("/calendar", "View today or this week’s calendar"),
                        ("/exit", "End the current session"),
                    ],
                },
            )
        if cmd == "/memory":
            content = self._current_memory_content()
            describe_memory = getattr(self.language, "describe_memory_preferences", None)
            if callable(describe_memory):
                message = describe_memory(content)
            else:
                message = content or "PROPHET.md is empty."
            return ChatResponse(session_id=state.session.session_id, message=message, metadata={"memory": content})
        if cmd == "/remember":
            rule = command[len("/remember") :].strip()
            return self._remember_rule(state, rule)
        if cmd == "/forget":
            rule = command[len("/forget") :].strip()
            return self._forget_rule(state, rule)
        if cmd == "/model":
            if len(parts) == 1:
                return ChatResponse(
                    session_id=state.session.session_id,
                    message="Choose the active model for this session.",
                    metadata={
                        "view": "model_picker",
                        "current": state.session.model_override or "auto",
                        "options": self._model_options(),
                    },
                )

            target = parts[1].lower().replace("default", "auto").replace("reset", "auto")
            if target in {"auto", "gemini", "openai"}:
                state.session.model_override = normalize_model_override(target)
                if self.agent_runtime:
                    self.agent_runtime.model_override = normalize_model_override(target)
                return ChatResponse(
                    session_id=state.session.session_id,
                    message=f"Model switched to {target} for this session.",
                    metadata={
                        "view": "model_picker",
                        "current": target,
                        "options": self._model_options(),
                    },
                )

            return ChatResponse(
                session_id=state.session.session_id,
                message="Unknown model option. Use /model, /model auto, /model gemini, or /model openai.",
            )
        if cmd == "/pairs":
            if len(parts) >= 3 and parts[1].lower() in {"add", "remove"}:
                pair = normalize_pair_alias(parts[2]) or parts[2].upper()
                route = RouteDecision(
                    intent="config_add_pair" if parts[1].lower() == "add" else "config_remove_pair",
                    pair=pair,
                )
                return self._handle_config_mutation(state, route, authorize_mutation)
            pairs = self.config_manager.show_pairs()
            return ChatResponse(
                session_id=state.session.session_id,
                message="Choose a watchlist action.",
                metadata={
                    "view": "pairs_picker",
                    "pairs": pairs,
                    "actions": ["View current pairs", "Add a pair", "Remove a pair"],
                },
            )
        if cmd == "/sessions":
            return self._sessions_response(state, parts[1:] if len(parts) > 1 else [])
        if cmd == "/calendar":
            return self._calendar_response(state, parts[1:] if len(parts) > 1 else [])
        if cmd == "/exit":
            return ChatResponse(session_id=state.session.session_id, message="Closing chat session.", should_exit=True)
        return ChatResponse(session_id=state.session.session_id, message=f"Unknown command: {cmd}")

    def _routing_context(self, state: ChatSessionState) -> dict:
        recent_turns = state.session.turns[-self.settings.context.max_history_turns :]
        return {
            "active_pair": state.session.context.active_pair,
            "last_intent": state.session.context.last_intent,
            "recent_turns": [
                {"role": turn.role, "content": turn.content, "metadata": turn.metadata}
                for turn in recent_turns
            ],
            "configured_pairs": self.settings.trading.pairs,
            "default_risk_pct": self.settings.trading.risk.default_risk_pct,
        }

    def _current_memory_content(self) -> str:
        if self.memory_repository is None:
            return ""
        return self.memory_repository.get_content()

    def _remember_rule(self, state: ChatSessionState, rule: str) -> ChatResponse:
        if not rule:
            return ChatResponse(session_id=state.session.session_id, message="Usage: /remember [rule]")
        if self.memory_repository is None:
            return ChatResponse(session_id=state.session.session_id, message="Memory storage is unavailable.")
        state.session.context.pending_forget_matches = []
        content, ok = self.memory_repository.add_rule(rule, self.settings.memory.max_characters)
        if not ok:
            return ChatResponse(
                session_id=state.session.session_id,
                message=(
                    f"PROPHET.md is limited to {self.settings.memory.max_characters} characters. "
                    "Remove older rules before adding more."
                ),
                metadata={"memory": self.memory_repository.get_content()},
            )
        return ChatResponse(
            session_id=state.session.session_id,
            message=f"Remembered: {rule}",
            metadata={"memory": content},
        )

    def _forget_rule(self, state: ChatSessionState, rule: str) -> ChatResponse:
        if not rule:
            return ChatResponse(session_id=state.session.session_id, message="Usage: /forget [rule]")
        if self.memory_repository is None:
            return ChatResponse(session_id=state.session.session_id, message="Memory storage is unavailable.")
        pending_matches = list(state.session.context.pending_forget_matches)
        if pending_matches and rule.isdigit():
            index = int(rule) - 1
            if 0 <= index < len(pending_matches):
                selected = pending_matches[index]
                content = self.memory_repository.forget_rules([selected])
                state.session.context.pending_forget_matches = []
                return ChatResponse(
                    session_id=state.session.session_id,
                    message=f"Forgot: {selected}",
                    metadata={"memory": content},
                )
            return ChatResponse(
                session_id=state.session.session_id,
                message=f"Choose a number between 1 and {len(pending_matches)} to remove one memory rule.",
                metadata={"memory": self.memory_repository.get_content(), "matches": pending_matches},
            )
        state.session.context.pending_forget_matches = []
        find_matches = getattr(self.memory_repository, "find_matching_rules", None)
        if callable(find_matches):
            matches = find_matches(rule)
        else:
            content_text = self.memory_repository.get_content()
            matches = [
                (line[2:] if line.startswith("- ") else line).strip()
                for line in content_text.splitlines()
                if rule.lower() in line.lower()
            ]
        if not matches:
            return ChatResponse(
                session_id=state.session.session_id,
                message=f'No memory rules matched "{rule}".',
                metadata={"memory": self.memory_repository.get_content()},
            )
        if len(matches) > 1:
            state.session.context.pending_forget_matches = matches
            options = "\n".join(f"{index}. {item}" for index, item in enumerate(matches, start=1))
            return ChatResponse(
                session_id=state.session.session_id,
                message=(
                    f'Multiple memory rules matched "{rule}". Reply with /forget [number] to remove one:\n{options}'
                ),
                metadata={"memory": self.memory_repository.get_content(), "matches": matches},
            )
        forget_rules = getattr(self.memory_repository, "forget_rules", None)
        if callable(forget_rules):
            content = forget_rules(matches)
        else:
            content = self.memory_repository.forget_rule(matches[0])
        return ChatResponse(
            session_id=state.session.session_id,
            message=f"Forgot: {matches[0]}",
            metadata={"memory": content},
        )

    def _sessions_response(self, state: ChatSessionState, args: list[str]) -> ChatResponse:
        try:
            sessions = self.session_store.list_recent()
        except Exception:  # noqa: BLE001
            return ChatResponse(session_id=state.session.session_id, message="Session history is unavailable.")
        if not sessions:
            return ChatResponse(session_id=state.session.session_id, message="No saved sessions yet.")
        if args:
            target = args[0]
            if target.isdigit():
                index = int(target) - 1
                if 0 <= index < len(sessions):
                    selected = sessions[index]
                    return ChatResponse(
                        session_id=state.session.session_id,
                        message=f"Resume session {index + 1} with your client or selector.",
                        metadata={"resume_session_id": selected.id},
                    )
        lines = []
        for index, item in enumerate(sessions, start=1):
            started = item.started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
            summary = item.summary or "No summary yet."
            lines.append(f"{index}. {started}  {summary}")
        return ChatResponse(
            session_id=state.session.session_id,
            message="\n".join(lines),
            metadata={
                "view": "sessions_picker",
                "sessions": [item.model_dump(mode="json") for item in sessions],
            },
        )

    def _calendar_response(self, state: ChatSessionState, args: list[str]) -> ChatResponse:
        if self.calendar_service is None:
            return ChatResponse(session_id=state.session.session_id, message="Calendar provider is unavailable.")
        requested = args[0].lower() if args else self.settings.calendar.default_view
        if requested not in {"today", "week"}:
            requested = self.settings.calendar.default_view
        data = self.calendar_service.get_events(requested, self.config_manager.show_pairs())
        if data.events:
            event_lines = [f"{item.date} {item.time_utc} UTC | {item.currency} | {item.impact} | {item.event_name}" for item in data.events]
            warning_lines = [f"Warning: {item.message}" for item in data.warnings]
            message = "\n".join(event_lines + warning_lines)
        elif data.warnings:
            message = "\n".join(f"Warning: {item.message}" for item in data.warnings)
        else:
            message = "No calendar events returned for that view."
        return ChatResponse(
            session_id=state.session.session_id,
            message=message,
            metadata={**data.model_dump(mode="json"), "view": "calendar_picker"},
        )

    def _agent_messages(
        self,
        state: ChatSessionState,
        current_message: str,
        image_attachments: list[dict[str, str]] | None = None,
    ) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        recent_turns = state.session.turns[-self.settings.context.max_history_turns :]
        for turn in recent_turns:
            if turn.role not in {"user", "assistant"}:
                continue
            content = self._agent_history_content(turn)
            if not content:
                continue
            messages.append({"role": turn.role, "content": content})
        messages.append({"role": "user", "content": self._current_user_content(current_message, image_attachments)})
        return messages

    def _current_user_content(
        self,
        current_message: str,
        image_attachments: list[dict[str, str]] | None,
    ) -> str | list[dict[str, str]]:
        if not image_attachments:
            return current_message
        prompt = self._chart_analysis_prompt(current_message, len(image_attachments))
        content_blocks = [
            {
                "type": "image",
                "base64": attachment["base64"],
                "mime_type": attachment["media_type"],
            }
            for attachment in image_attachments
        ]
        content_blocks.append({"type": "text", "text": prompt})
        return content_blocks

    def _chart_analysis_prompt(self, current_message: str, attachment_count: int) -> str:
        template = MULTI_TIMEFRAME_ANALYSIS_PROMPT if attachment_count > 1 else SINGLE_CHART_ANALYSIS_PROMPT
        return template.format(user_message=current_message)

    def _agent_history_content(self, turn: ChatTurn) -> str:
        if turn.role == "user":
            return " ".join(turn.content.strip().split())
        content = self._compact_history_text(turn.content)
        return self._assistant_history_content(turn, content)

    def _assistant_history_content(self, turn: ChatTurn, content: str) -> str:
        if content:
            return content
        summaries = turn.metadata.get("tool_summaries")
        if not summaries:
            return ""
        latest = next((str(item).strip() for item in reversed(summaries) if str(item).strip()), "")
        return self._compact_history_text(latest)

    def _compact_history_text(self, content: str, limit: int = 240) -> str:
        compact = " ".join(content.strip().split())
        if len(compact) <= limit:
            return compact
        trimmed = compact[: limit - 3].rstrip(" ,.;:")
        return f"{trimmed}..."

    def _answer_context(self, state: ChatSessionState, route: RouteDecision) -> dict:
        context = self._routing_context(state)
        context["question"] = route.question
        context["pair"] = route.pair
        return context

    def _agent_system_prompt(self, state: ChatSessionState) -> str:
        session_state = current_session_status(self.settings.trading.sessions)
        context = state.session.context
        today = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        memory = self._current_memory_content().strip()
        style_modifier = get_style_modifier(self._current_experience_level())
        prompt = (
            "You are Prophet, a concise forex trading CLI assistant. "
            "Use tools when the answer depends on live market structure, watchlist settings, session timing, risk, calendar events, or live news. "
            "Use get_economic_calendar for economic event and calendar questions before using web_search. "
            "Use internal tools instead of web search for bias, setup, session, watchlist, or risk calculations. "
            "Use generate_trade_plan when the user asks for a trade plan, plan this trade, generate a plan, gives you an entry with a stop loss, or asks for lot size in the context of a planned trade. "
            "Use show_memory whenever trader rules or prior preferences matter, and respect those rules in recommendations. "
            "Use the existing conversation context for follow-up questions when it already supplies the instrument or setup, and avoid unnecessary tool calls. "
            "When the current user turn includes chart images, analyse the chart visually before answering. Lead with what Prophet sees, then market structure, key levels, bias, go/no-go, and what to watch. "
            "For chart-image turns, identify support/resistance, FVGs, Fib 0.5 to 0.786 if visible, liquidity zones, timeframe context, and confluence, then cross-reference the trader's PROPHET.md rules before the final verdict. "
            "When two chart images are provided, treat the request as multi-timeframe analysis and assess whether the higher and lower timeframe views align. "
            "When generate_trade_plan succeeds, the final answer should match the generated narrative and formatted block without extra markdown. "
            "If a tool reports an error or blocked mutation, explain it briefly and continue with the best partial answer. "
            "Keep final answers short, practical, and trader-focused.\n"
            f"Current time: {today}\n"
            f"Configured pairs: {', '.join(self.settings.trading.pairs)}\n"
            f"Active pair: {context.active_pair or 'None'}\n"
            f"Last intent: {context.last_intent or 'None'}\n"
            f"Session status: {session_state['status']}\n"
        )
        if memory:
            prompt += f"Trader memory (PROPHET.md):\n{memory}\n"
        prompt += f"{style_modifier}\n"
        if state.session.append_system_prompt:
            prompt += state.session.append_system_prompt.strip()
        return prompt.strip()

    def _current_profile_record(self):
        if self.user_profile_repository is None or not self.device_token:
            return None
        return self.user_profile_repository.get_by_device_token(self.device_token)

    def _current_experience_level(self) -> str:
        profile = self._current_profile_record()
        if profile is None:
            return "intermediate"
        return str(profile.experience_level or "intermediate").strip().lower() or "intermediate"

    def _resolve_style_confirmation(self, state: ChatSessionState, content: str) -> tuple[str, str]:
        decision, remainder = self._extract_confirmation_decision(content)
        if decision == "yes":
            new_level = state.session.context.suggested_experience_level or self._current_experience_level()
            self._update_profile(experience_level=new_level)
            self._clear_style_suggestion(state, made=True)
            return f"Done. I have updated your profile to {new_level} style.", remainder
        if decision == "no":
            current_level = self._current_experience_level()
            self._clear_style_suggestion(state, made=True)
            return f"Understood. I will continue with your current {current_level} style.", remainder
        self._clear_style_suggestion(state, made=True)
        return "", content

    def _extract_confirmation_decision(self, content: str) -> tuple[str | None, str]:
        normalized = content.strip().lower()
        yes_signals = ("yes", "sure", "ok", "okay", "go ahead", "update", "change it", "sounds good", "do it")
        no_signals = ("no", "nope", "keep", "stay", "don't change", "leave it", "not now")
        for signal in yes_signals:
            if normalized == signal or normalized.startswith(f"{signal} ") or normalized.startswith(f"{signal},") or normalized.startswith(f"{signal}."):
                remainder = content[len(signal):].lstrip(" ,.-")
                return "yes", remainder
        for signal in no_signals:
            if normalized == signal or normalized.startswith(f"{signal} ") or normalized.startswith(f"{signal},") or normalized.startswith(f"{signal}."):
                remainder = content[len(signal):].lstrip(" ,.-")
                return "no", remainder
        return None, content

    def _finalize_adaptive_style_response(
        self,
        state: ChatSessionState,
        original_content: str,
        response: ChatResponse,
        confirmation_prefix: str,
        pending_confirmation: bool,
    ) -> ChatResponse:
        if not pending_confirmation:
            self._track_skill_signals(state, original_content, response)
        response = self._with_style_prefix(response, confirmation_prefix)
        return self._with_style_suggestion(state, response)

    def _with_style_prefix(self, response: ChatResponse, prefix: str) -> ChatResponse:
        if not prefix:
            return response
        message = response.message or ""
        response.message = f"{prefix}\n{message}".strip()
        return response

    def _track_skill_signals(self, state: ChatSessionState, content: str, response: ChatResponse) -> None:
        context = state.session.context
        normalized = " ".join(content.strip().split())
        if not normalized:
            return
        context.recent_user_messages.append(normalized)
        if len(context.recent_user_messages) < 3 or context.style_suggestion_made or context.style_suggestion_pending:
            return
        assessment = detect_skill_signals(context.recent_user_messages, current_level=self._current_experience_level())
        if not assessment.get("should_suggest"):
            return
        context.style_suggestion_pending = True
        context.suggested_experience_level = str(assessment.get("suggested_level") or "intermediate")
        observed = assessment.get("observed_advanced_terms") or assessment.get("observed_beginner_signals") or []
        context.suggestion_observed_terms = [str(item) for item in observed[:3]]
        response.metadata["adaptive_style"] = {
            "suggested_level": context.suggested_experience_level,
            "observed_terms": list(context.suggestion_observed_terms),
            "confidence": assessment.get("confidence"),
        }

    def _with_style_suggestion(self, state: ChatSessionState, response: ChatResponse) -> ChatResponse:
        context = state.session.context
        if not context.style_suggestion_pending:
            return response
        suggestion = self._style_suggestion_text(context)
        base = (response.message or "").rstrip()
        response.message = f"{base}\n\n---\n{suggestion}\n---".strip()
        return response

    def _style_suggestion_text(self, context) -> str:
        current_level = self._current_experience_level()
        suggested_level = context.suggested_experience_level or "intermediate"
        observed_terms = list(context.suggestion_observed_terms)
        if observed_terms:
            observed_text = ", ".join(observed_terms)
            first_line = (
                f"I notice you are comfortable with {observed_text}."
                if suggested_level in {"experienced", "professional"}
                else "I notice you have been asking about some foundational concepts."
            )
        else:
            first_line = (
                "I notice you are using advanced trading language naturally."
                if suggested_level in {"experienced", "professional"}
                else "I notice you have been asking about some foundational concepts."
            )
        change_map = {
            "beginner": "more step-by-step explanations with plain-language definitions.",
            "intermediate": "a bit more reasoning and context as we go.",
            "experienced": "more direct responses with less step-by-step explanation.",
            "professional": "higher-density responses focused on signal, confluence, levels, and risk.",
        }
        return (
            f"{first_line}\n"
            f"Your profile is currently set to {current_level} - would you like me to adjust how I communicate to match {suggested_level} style?\n"
            f"This means {change_map.get(suggested_level, change_map['intermediate'])}\n\n"
            "Reply yes to update or no to keep your current style."
        )

    def _clear_style_suggestion(self, state: ChatSessionState, made: bool) -> None:
        context = state.session.context
        context.style_suggestion_pending = False
        context.style_suggestion_made = made
        context.suggested_experience_level = None
        context.suggestion_observed_terms = []

    def _update_profile(self, **changes):
        if self.user_profile_repository is None or not self.device_token:
            return None
        record = self.user_profile_repository.get_by_device_token(self.device_token)
        if record is None:
            return None
        next_values = {
            "display_name": record.display_name,
            "experience_level": changes.get("experience_level", record.experience_level),
            "watchlist": changes.get("watchlist", list(record.watchlist or [])),
            "account_balance": changes.get("account_balance", record.account_balance),
            "risk_pct": changes.get("risk_pct", record.risk_pct),
            "min_rr": changes.get("min_rr", record.min_rr),
            "sessions": changes.get("sessions", list(record.sessions or [])),
        }
        if "experience_level" in changes:
            changes["prophet_md"] = generate_prophet_md(SimpleNamespace(**next_values))
        return self.user_profile_repository.update_by_device_token(self.device_token, **changes)

    def _record(self, state: ChatSessionState, user_message: str, response: ChatResponse) -> ChatResponse:
        if user_message:
            self.session_store.add_turn(
                state,
                ChatTurn(role="user", content=user_message, route=response.route),
            )
        assistant_text = response.message or ""
        self.session_store.add_turn(
            state,
            ChatTurn(role="assistant", content=assistant_text, route=response.route, metadata=response.metadata),
        )
        if response.should_exit:
            self._finalize_session(state)
        return response

    def _finalize_session(self, state: ChatSessionState) -> None:
        state.session.ended_at = datetime.now(tz=UTC)
        if self.settings.sessions.auto_summary:
            turns = [
                {"role": turn.role, "content": turn.content, "metadata": turn.metadata}
                for turn in state.session.turns
            ]
            summarize = getattr(self.language, "summarize_session", None)
            if callable(summarize):
                state.session.summary = summarize(turns)
            elif turns:
                state.session.summary = turns[-1]["content"]
        if hasattr(self.session_store, "finalize"):
            self.session_store.finalize(state)
        else:
            self.session_store.save(state)

    def _resolve_pairs(self, state: ChatSessionState, route: RouteDecision) -> list[str]:
        if route.scope == "all":
            return self.settings.trading.pairs
        if route.pair:
            return [route.pair]
        if state.session.context.active_pair:
            return [state.session.context.active_pair]
        return self.settings.trading.pairs

    def _refresh_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.scan_service.settings = settings
        self.language.settings = settings
        if self.agent_runtime:
            self.agent_runtime.settings = settings

    def _match_fast_config_command(self, message: str) -> RouteDecision | None:
        normalized = message.strip().lower()
        patterns = (
            (r"^(?:add|watch|track)\s+(?P<pair>[a-z/ ]+?)\s+(?:to\s+)?(?:my\s+)?(?:watchlist|pairs?)$", "config_add_pair"),
            (r"^(?:remove|unwatch|drop)\s+(?P<pair>[a-z/ ]+?)\s+(?:from\s+)?(?:my\s+)?(?:watchlist|pairs?)$", "config_remove_pair"),
        )
        for pattern, intent in patterns:
            match = re.match(pattern, normalized)
            if not match:
                continue
            pair = normalize_pair_alias(match.group("pair"))
            if pair:
                return RouteDecision(intent=intent, pair=pair)
        return None

    def _model_options(self) -> list[tuple[str, str, str]]:
        return [
            ("auto", f"Gemini -> OpenAI fallback ({self.settings.ai.models.gemini} / {self.settings.ai.models.openai})", "Best default for most sessions"),
            ("gemini", self.settings.ai.models.gemini, "Fast market reasoning with Gemini only"),
            ("openai", self.settings.ai.models.openai, "Use OpenAI only for this session"),
        ]

    def _missing_fields_message(self, route: RouteDecision) -> str:
        labels = {
            "pair": "a pair like XAUUSD or EURUSD",
            "sl_pips": "a stop-loss in pips",
            "lot_size": "a lot size",
        }
        wanted = [labels.get(item, item) for item in route.missing_fields]
        return "I need " + " and ".join(wanted) + " to do that."
