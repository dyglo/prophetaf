from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain.tools import tool

from hedge_fund.chat.agent_runtime import AgentArtifacts
from hedge_fund.chat.models import ChatSessionState
from hedge_fund.chat.scratchpad import ScratchpadLogger
from hedge_fund.chat.utils import current_session_status, normalize_pair_alias
from hedge_fund.config.settings import Settings
from hedge_fund.integrations.search.tavily import TavilySearchClient
from hedge_fund.services.scan_service import RiskService, ScanService
from hedge_fund.services.trade_plan_service import TradePlanService
from hedge_fund.tools.run_backtest import run_backtest_payload


@dataclass
class AgentToolContext:
    settings: Settings
    state: ChatSessionState
    scan_service: ScanService
    risk_service: RiskService
    reverse_risk_service: Any
    config_manager: Any
    search_client: TavilySearchClient | None
    scratchpad: ScratchpadLogger
    artifacts: AgentArtifacts
    memory_repository: Any = None
    calendar_service: Any = None
    authorize_mutation: Callable[[str], bool] | None = None
    tool_summary_handler: Callable[[str, dict[str, Any], dict[str, Any]], str] | None = None

    def build_tools(self) -> list[Any]:
        @tool
        def get_market_bias(pair: str = "", all_pairs: bool = False) -> str:
            """Return the current H1 market bias for one pair or all configured pairs."""
            return self._run_tool("get_market_bias", {"pair": pair, "all_pairs": all_pairs}, self._get_market_bias, pair, all_pairs)

        @tool
        def scan_setups(pair: str = "", all_pairs: bool = False, score_threshold: int = 0) -> str:
            """Run the setup scanner for one pair or all pairs and optionally filter by minimum score."""
            return self._run_tool(
                "scan_setups",
                {"pair": pair, "all_pairs": all_pairs, "score_threshold": score_threshold},
                self._scan_setups,
                pair,
                all_pairs,
                score_threshold,
            )

        @tool
        def calculate_risk(pair: str = "", sl_pips: int = 0, risk_pct: float = 0.0) -> str:
            """Calculate lot size and targets for a pair using stop-loss pips and risk percent."""
            return self._run_tool(
                "calculate_risk",
                {"pair": pair, "sl_pips": sl_pips, "risk_pct": risk_pct},
                self._calculate_risk,
                pair,
                sl_pips,
                risk_pct,
            )

        @tool
        def calculate_risk_exposure(pair: str = "", lot_size: float = 0.0, sl_pips: int = 0) -> str:
            """Calculate percentage account exposure from an existing lot size and stop-loss."""
            return self._run_tool(
                "calculate_risk_exposure",
                {"pair": pair, "lot_size": lot_size, "sl_pips": sl_pips},
                self._calculate_risk_exposure,
                pair,
                lot_size,
                sl_pips,
            )

        @tool
        def generate_trade_plan(
            pair: str = "",
            direction: str = "",
            entry: float = 0.0,
            stop_loss: float = 0.0,
            setup_type: str = "",
            session: str = "",
            confluence_score: int = 0,
            risk_pct: float = 0.0,
        ) -> str:
            """Generate a full trade plan with lot size, targets, rule checks, and a terminal-ready block."""
            return self._run_tool(
                "generate_trade_plan",
                {
                    "pair": pair,
                    "direction": direction,
                    "entry": entry,
                    "stop_loss": stop_loss,
                    "setup_type": setup_type,
                    "session": session,
                    "confluence_score": confluence_score,
                    "risk_pct": risk_pct,
                },
                self._generate_trade_plan,
                pair,
                direction,
                entry,
                stop_loss,
                setup_type,
                session,
                confluence_score,
                risk_pct,
            )

        @tool
        def get_session_status() -> str:
            """Return whether the forex market is open, which session is active, and time until next open."""
            return self._run_tool("get_session_status", {}, self._get_session_status)

        @tool
        def web_search(query: str) -> str:
            """Search the live web for forex news, macro events, fundamentals, and market-moving headlines."""
            return self._run_tool("web_search", {"query": query}, self._web_search, query)

        @tool
        def get_economic_calendar(view: str = "today", pair: str = "") -> str:
            """Return structured economic calendar events for today or this week, and flag affected pairs."""
            return self._run_tool(
                "get_economic_calendar",
                {"view": view, "pair": pair},
                self._get_economic_calendar,
                view,
                pair,
            )

        @tool
        def rank_watchlist_pairs() -> str:
            """Scan configured watchlist pairs, rank them by setup quality, and suggest the best focus pair."""
            return self._run_tool("rank_watchlist_pairs", {}, self._rank_watchlist_pairs)

        @tool
        def show_memory() -> str:
            """Show the current PROPHET.md memory rules and trader preferences."""
            return self._run_tool("show_memory", {}, self._show_memory)

        @tool
        def remember_rule(rule: str) -> str:
            """Add a new trader rule or preference to PROPHET.md when the user explicitly asks to remember it."""
            return self._run_tool("remember_rule", {"rule": rule}, self._remember_rule, rule)

        @tool
        def forget_rule(rule: str) -> str:
            """Remove a trader rule or preference from PROPHET.md when the user explicitly asks to forget it."""
            return self._run_tool("forget_rule", {"rule": rule}, self._forget_rule, rule)

        @tool
        def show_watchlist() -> str:
            """Return the currently configured watchlist pairs."""
            return self._run_tool("show_watchlist", {}, self._show_watchlist)

        @tool
        def get_watchlist() -> str:
            """Return the currently configured watchlist pairs."""
            return self._run_tool("get_watchlist", {}, self._show_watchlist)

        @tool
        def show_risk_settings() -> str:
            """Return the configured default risk and target risk-reward settings."""
            return self._run_tool("show_risk_settings", {}, self._show_risk_settings)

        @tool
        def add_watchlist_pair(pair: str) -> str:
            """Add a forex pair to the watchlist when the user explicitly asks for it."""
            return self._run_tool("add_watchlist_pair", {"pair": pair}, self._add_watchlist_pair, pair)

        @tool
        def remove_watchlist_pair(pair: str) -> str:
            """Remove a forex pair from the watchlist when the user explicitly asks for it."""
            return self._run_tool("remove_watchlist_pair", {"pair": pair}, self._remove_watchlist_pair, pair)

        @tool
        def run_backtest(
            pair: str,
            granularity: str = "H1",
            from_date: str = "",
            to_date: str = "",
            account_size: float = 10000.0,
        ) -> str:
            """Run a backtest of Prophet's FVG + Fibonacci + liquidity sweep strategy on historical data for a given pair."""
            return self._run_tool(
                "run_backtest",
                {
                    "pair": pair,
                    "granularity": granularity,
                    "from_date": from_date,
                    "to_date": to_date,
                    "account_size": account_size,
                },
                self._run_backtest,
                pair,
                granularity,
                from_date,
                to_date,
                account_size,
            )

        return [
            get_market_bias,
            scan_setups,
            calculate_risk,
            calculate_risk_exposure,
            generate_trade_plan,
            get_session_status,
            web_search,
            get_economic_calendar,
            rank_watchlist_pairs,
            show_memory,
            remember_rule,
            forget_rule,
            show_watchlist,
            get_watchlist,
            show_risk_settings,
            add_watchlist_pair,
            remove_watchlist_pair,
            run_backtest,
        ]

    def _run_tool(self, name: str, arguments: dict[str, Any], handler, *args) -> str:
        try:
            result = handler(*args)
        except Exception as exc:  # noqa: BLE001
            result = {
                "ok": False,
                "tool": name,
                "error": str(exc),
            }
        llm_summary = self._tool_summary(name, arguments, result)
        self.scratchpad.log_tool_result(name, arguments, result, llm_summary)
        return json.dumps(result, default=str)

    def _tool_summary(self, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> str:
        if self.tool_summary_handler is not None:
            try:
                summary = self.tool_summary_handler(name, arguments, result)
                if summary and summary.strip():
                    return summary.strip()
            except Exception:  # noqa: BLE001
                pass
        fallback = str(result.get("summary") or result.get("recommendation") or "").strip()
        if fallback:
            return fallback if fallback.endswith((".", "!", "?")) else f"{fallback}."
        if result.get("ok") is False:
            error = str(result.get("error") or "the tool returned an issue").strip()
            return f"{name.replace('_', ' ')} hit an issue: {error}."
        return f"{name.replace('_', ' ')} completed without a standout signal."

    def _get_market_bias(self, pair: str, all_pairs: bool) -> dict[str, Any]:
        pairs = self._resolve_pairs(pair, all_pairs)
        biases = self.scan_service.bias_only(pairs)
        self.artifacts.biases = biases
        self._set_pair_context(pairs, "bias")
        summary = ", ".join(f"{item.pair}: {item.bias}" for item in biases)
        self.artifacts.summaries.append(f"Bias: {summary}")
        return {
            "ok": True,
            "pairs": pairs,
            "biases": [item.model_dump(mode="json") for item in biases],
            "summary": summary,
        }

    def _scan_setups(self, pair: str, all_pairs: bool, score_threshold: int) -> dict[str, Any]:
        pairs = self._resolve_pairs(pair, all_pairs)
        bundle = self.scan_service.scan(pairs)
        setups = bundle.setups
        biases = bundle.biases
        if score_threshold > 0:
            setups = [item for item in setups if item.score >= score_threshold]
            matching_pairs = {item.pair for item in setups}
            biases = [item for item in biases if item.pair in matching_pairs]
        self.artifacts.biases = biases
        self.artifacts.setups = setups
        self.artifacts.ai_analysis = bundle.ai_analysis
        self._set_pair_context(pairs, "scan", setups)
        if setups:
            summary = ", ".join(f"{item.pair}: {item.score}/10 {item.direction}" for item in setups)
        else:
            summary = "No setups met the requested threshold."
        self.artifacts.summaries.append(f"Setups: {summary}")
        return {
            "ok": True,
            "pairs": pairs,
            "biases": [item.model_dump(mode="json") for item in biases],
            "setups": [item.model_dump(mode="json") for item in setups],
            "ai_analysis": [item.model_dump(mode="json") for item in bundle.ai_analysis],
            "summary": summary,
        }

    def _calculate_risk(self, pair: str, sl_pips: int, risk_pct: float) -> dict[str, Any]:
        resolved_pair = self._resolve_pair(pair)
        calculation = self.risk_service.calculate(
            resolved_pair,
            risk_pct or self.settings.trading.risk.default_risk_pct,
            sl_pips,
        )
        self.artifacts.risk = calculation
        self.state.session.context.active_pair = calculation.pair
        self.state.session.context.last_intent = "risk_size"
        summary = f"{calculation.pair}: {calculation.lot_size:.4f} lots at {calculation.risk_pct:.2f}% risk."
        self.artifacts.summaries.append(f"Risk: {summary}")
        return {
            "ok": True,
            "risk": calculation.model_dump(mode="json"),
            "summary": summary,
        }

    def _calculate_risk_exposure(self, pair: str, lot_size: float, sl_pips: int) -> dict[str, Any]:
        resolved_pair = self._resolve_pair(pair)
        calculation = self.reverse_risk_service.calculate(resolved_pair, lot_size, sl_pips)
        self.artifacts.reverse_risk = calculation
        self.state.session.context.active_pair = calculation.pair
        self.state.session.context.last_intent = "risk_exposure"
        summary = f"{calculation.pair}: {calculation.risk_pct:.2f}% account exposure."
        self.artifacts.summaries.append(f"Exposure: {summary}")
        return {
            "ok": True,
            "reverse_risk": calculation.model_dump(mode="json"),
            "summary": summary,
        }

    def _generate_trade_plan(
        self,
        pair: str,
        direction: str,
        entry: float,
        stop_loss: float,
        setup_type: str,
        session: str,
        confluence_score: int,
        risk_pct: float,
    ) -> dict[str, Any]:
        resolved_pair = self._resolve_pair(pair)
        trade_plan_service = TradePlanService(
            self.risk_service.broker,
            self.risk_service.calculator,
        )
        plan = trade_plan_service.generate(
            resolved_pair,
            direction,
            entry,
            stop_loss,
            setup_type,
            session,
            confluence_score,
            risk_pct or self.settings.trading.risk.default_risk_pct,
        )
        memory_content = self.memory_repository.get_content() if self.memory_repository is not None else ""
        self.artifacts.trade_plan = plan
        self.artifacts.metadata["trade_plan"] = plan.model_dump(mode="json")
        if memory_content:
            self.artifacts.metadata["memory"] = memory_content
        self.state.session.context.active_pair = plan.pair
        self.state.session.context.last_intent = "trade_plan"
        summary = f"{plan.pair} {plan.direction}: {plan.lot_size:.2f} lots with TP1 at {plan.tp1:.2f} and TP2 at {plan.tp2:.2f}."
        self.artifacts.summaries.append(f"Trade plan: {summary}")
        return {
            "ok": True,
            "trade_plan": plan.model_dump(mode="json"),
            "summary": summary,
        }

    def _get_session_status(self) -> dict[str, Any]:
        status = current_session_status(self.settings.trading.sessions)
        self.artifacts.metadata["session_status"] = status
        self.artifacts.summaries.append(f"Session: {status['status']}")
        return {
            "ok": True,
            "session": status,
            "summary": status["status"],
        }

    def _web_search(self, query: str) -> dict[str, Any]:
        if not self.search_client:
            raise ValueError("Web search is not configured.")
        result = self.search_client.search(query)
        self.artifacts.metadata["web_search"] = result
        urls = [item["url"] for item in result["results"] if item.get("url")]
        summary = result["summary"]
        self.artifacts.summaries.append(f"Search: {summary}")
        return {
            "ok": True,
            "search": result,
            "sources": urls[:3],
            "summary": summary,
        }

    def _show_watchlist(self) -> dict[str, Any]:
        pairs = self.config_manager.show_pairs()
        summary = ", ".join(pairs)
        self.artifacts.metadata["watchlist"] = pairs
        self.artifacts.summaries.append(f"Watchlist: {summary}")
        return {
            "ok": True,
            "pairs": pairs,
            "summary": summary,
        }

    def _show_risk_settings(self) -> dict[str, Any]:
        settings = self.config_manager.show_risk()
        summary = (
            f"Default risk {settings['default_risk_pct']}%, "
            f"minimum RR {settings['minimum_rr']}, preferred RR {settings['preferred_rr']}."
        )
        self.artifacts.metadata["risk_settings"] = settings
        self.artifacts.summaries.append(f"Risk settings: {summary}")
        return {
            "ok": True,
            "risk_settings": settings,
            "summary": summary,
        }

    def _get_economic_calendar(self, view: str, pair: str) -> dict[str, Any]:
        if self.calendar_service is None:
            raise ValueError("Economic calendar is not configured.")
        requested_pairs = [self._resolve_pair(pair)] if pair else self.config_manager.show_pairs()
        response = self.calendar_service.get_events(view.lower() if view else "today", requested_pairs)
        self.artifacts.metadata["calendar"] = response.model_dump(mode="json")
        if response.events:
            summary = ", ".join(
                (
                    f"{self._calendar_value(item, 'currency')} "
                    f"{self._calendar_value(item, 'event_name')} "
                    f"{self._calendar_value(item, 'time_utc')} UTC"
                )
                for item in response.events[:5]
            )
        else:
            summary = "No economic events returned."
        self.artifacts.summaries.append(f"Calendar: {summary}")
        return {
            "ok": True,
            "calendar": response.model_dump(mode="json"),
            "summary": summary,
        }

    def _calendar_value(self, item: Any, field: str) -> str:
        if isinstance(item, dict):
            return str(item.get(field, ""))
        return str(getattr(item, field, ""))

    def _rank_watchlist_pairs(self) -> dict[str, Any]:
        pairs = self.config_manager.show_pairs()
        ranked: list[dict[str, Any]] = []
        bundles: dict[str, Any] = {}
        for pair in pairs:
            bundle = self.scan_service.scan([pair])
            bundles[pair] = bundle
            if not bundle.setups or not bundle.biases:
                continue
            setup = bundle.setups[0]
            bias = bundle.biases[0]
            ranked.append(
                {
                    "pair": pair,
                    "score": setup.score,
                    "bias": bias.bias,
                    "signals": setup.signals_summary,
                    "direction": setup.direction,
                }
            )
        ranked.sort(key=lambda item: item["score"], reverse=True)
        self.artifacts.metadata["ranking"] = ranked
        if ranked:
            self.artifacts.setups = [bundles[item["pair"]].setups[0] for item in ranked]
            self.artifacts.biases = [bundles[item["pair"]].biases[0] for item in ranked]
            self.artifacts.ai_analysis = [
                analysis
                for item in ranked
                for analysis in bundles[item["pair"]].ai_analysis
            ]
        best = ranked[0] if ranked else None
        if best and best["score"] >= self.settings.trading.scanner.minimum_score:
            summary = f"Best setup is {best['pair']} at {best['score']}/10."
        else:
            summary = "No pair is above the minimum setup threshold."
        self.artifacts.summaries.append(f"Ranking: {summary}")
        return {
            "ok": True,
            "ranking": ranked,
            "recommendation": summary,
            "summary": summary,
        }

    def _show_memory(self) -> dict[str, Any]:
        if self.memory_repository is None:
            raise ValueError("Memory storage is not configured.")
        content = self.memory_repository.get_content()
        summary = content if content else "No saved memory rules."
        self.artifacts.metadata["memory"] = content
        self.artifacts.summaries.append(f"Memory: {summary}")
        return {
            "ok": True,
            "content": content,
            "summary": summary,
        }

    def _remember_rule(self, rule: str) -> dict[str, Any]:
        if self.memory_repository is None:
            raise ValueError("Memory storage is not configured.")
        content, ok = self.memory_repository.add_rule(rule, self.settings.memory.max_characters)
        summary = (
            f"Remembered: {rule}"
            if ok
            else f"Memory is full at {self.settings.memory.max_characters} characters."
        )
        self.artifacts.metadata["memory"] = self.memory_repository.get_content()
        self.artifacts.summaries.append(f"Memory update: {summary}")
        return {
            "ok": ok,
            "content": content,
            "summary": summary,
        }

    def _forget_rule(self, rule: str) -> dict[str, Any]:
        if self.memory_repository is None:
            raise ValueError("Memory storage is not configured.")
        content = self.memory_repository.forget_rule(rule)
        summary = f"Forgot: {rule}"
        self.artifacts.metadata["memory"] = content
        self.artifacts.summaries.append(f"Memory update: {summary}")
        return {
            "ok": True,
            "content": content,
            "summary": summary,
        }

    def _add_watchlist_pair(self, pair: str) -> dict[str, Any]:
        return self._mutate_watchlist("add", pair)

    def _remove_watchlist_pair(self, pair: str) -> dict[str, Any]:
        return self._mutate_watchlist("remove", pair)

    def _run_backtest(
        self,
        pair: str,
        granularity: str,
        from_date: str,
        to_date: str,
        account_size: float,
    ) -> dict[str, Any]:
        resolved_pair = self._resolve_pair(pair)
        payload = run_backtest_payload(
            pair=resolved_pair,
            granularity=granularity or "H1",
            from_date=from_date,
            to_date=to_date,
            account_size=account_size or 10000.0,
            risk_pct=self.settings.trading.risk.default_risk_pct / 100,
        )
        self.artifacts.metadata["backtest"] = payload["backtest"]
        self.artifacts.metadata["backtest_report"] = payload["report"]
        self.state.session.context.active_pair = resolved_pair
        self.state.session.context.last_intent = "backtest"
        self.artifacts.summaries.append(f"Backtest: {payload['summary']}")
        return payload

    def _mutate_watchlist(self, action: str, pair: str) -> dict[str, Any]:
        resolved_pair = self._resolve_pair(pair)
        permission_mode = self.state.session.permission_mode
        if permission_mode == "plan":
            return {
                "ok": False,
                "summary": "Permission mode is plan, so config changes are blocked for this session.",
            }
        if permission_mode == "default":
            question = f"Update config.yaml for {resolved_pair}?"
            if self.authorize_mutation is None or not self.authorize_mutation(question):
                return {
                    "ok": False,
                    "summary": "Config change cancelled.",
                }

        settings = (
            self.config_manager.add_pair(resolved_pair)
            if action == "add"
            else self.config_manager.remove_pair(resolved_pair)
        )
        summary = f"{'Added' if action == 'add' else 'Removed'} {resolved_pair} in config.yaml."
        self.artifacts.metadata["watchlist"] = settings.trading.pairs
        self.artifacts.summaries.append(summary)
        self.state.session.context.last_intent = f"config_{action}_pair"
        return {
            "ok": True,
            "pairs": settings.trading.pairs,
            "summary": summary,
        }

    def _resolve_pairs(self, pair: str, all_pairs: bool) -> list[str]:
        if all_pairs:
            return self.settings.trading.pairs
        return [self._resolve_pair(pair)]

    def _resolve_pair(self, pair: str) -> str:
        if pair:
            normalized = normalize_pair_alias(pair)
            if normalized:
                return normalized
            return pair.upper()
        if self.state.session.context.active_pair:
            return self.state.session.context.active_pair
        return self.settings.trading.pairs[0]

    def _set_pair_context(
        self,
        pairs: list[str],
        intent: str,
        setups: list[SetupScanResult] | None = None,
    ) -> None:
        if len(pairs) == 1:
            self.state.session.context.active_pair = pairs[0]
            if intent == "scan":
                self.state.session.context.last_scan_pair = pairs[0]
        self.state.session.context.last_pairs = pairs
        self.state.session.context.last_intent = intent
        if intent == "bias":
            self.state.session.context.last_bias_pairs = pairs
        if intent == "scan":
            self.state.session.context.last_setup_pairs = [item.pair for item in (setups or [])]
