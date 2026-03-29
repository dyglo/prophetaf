from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AuthenticationError, OpenAI

from hedge_fund.chat.models import RouteDecision
from hedge_fund.chat.utils import normalize_model_override, normalize_pair_alias
from hedge_fund.config.environment import EnvironmentSettings
from hedge_fund.config.settings import Settings
from hedge_fund.domain.exceptions import ProviderError


class ChatLanguageService:
    def __init__(
        self,
        settings: Settings,
        env: EnvironmentSettings,
        logger: logging.Logger,
        model_override: str | None = None,
        append_system_prompt: str | None = None,
    ) -> None:
        self.settings = settings
        self.env = env
        self.logger = logger
        self.model_override = model_override
        self.append_system_prompt = append_system_prompt
        self.openai_client = OpenAI(api_key=env.openai_api_key, timeout=settings.chat.response_timeout_seconds)

    def route(self, message: str, context: dict) -> RouteDecision:
        failures: list[str] = []
        for provider_name, model in self._providers():
            try:
                raw = (
                    self._route_with_gemini(message, context, model)
                    if provider_name == "gemini"
                    else self._route_with_openai(message, context, model)
                )
                decision = RouteDecision.model_validate(raw)
                return self._post_process(message, decision, context)
            except ProviderError as exc:
                failures.append(f"{provider_name}: {exc}")
                self.logger.warning("Chat routing provider %s failed: %s", provider_name, exc)
        if failures:
            self.logger.warning("Falling back to heuristic chat routing: %s", "; ".join(failures))
        return self._post_process(message, self._heuristic_route(message, context), context)

    def answer_general(self, message: str, context: dict) -> str:
        failures: list[str] = []
        for provider_name, model in self._providers():
            try:
                if provider_name == "gemini":
                    return self._answer_with_gemini(message, context, model)
                return self._answer_with_openai(message, context, model)
            except ProviderError as exc:
                failures.append(f"{provider_name}: {exc}")
                self.logger.warning("General chat provider %s failed: %s", provider_name, exc)
        if failures:
            self.logger.warning("General chat fallback used: %s", "; ".join(failures))
        return self._heuristic_general_answer(message, context)

    def summarize_session(self, turns: list[dict]) -> str:
        failures: list[str] = []
        payload = {"turns": turns[-12:]}
        for provider_name, model in self._providers():
            try:
                if provider_name == "gemini":
                    return self._summarize_with_gemini(payload, model)
                return self._summarize_with_openai(payload, model)
            except ProviderError as exc:
                failures.append(f"{provider_name}: {exc}")
                self.logger.warning("Session summary provider %s failed: %s", provider_name, exc)
        if failures:
            self.logger.warning("Session summary fallback used: %s", "; ".join(failures))
        return self._heuristic_summary(turns)

    def describe_memory_preferences(self, content: str) -> str:
        text = content.strip()
        if not text:
            return "Your trading preferences are not saved yet."
        failures: list[str] = []
        payload = {"memory_rules": text}
        for provider_name, model in self._providers():
            try:
                if provider_name == "gemini":
                    return self._describe_memory_with_gemini(payload, model)
                return self._describe_memory_with_openai(payload, model)
            except ProviderError as exc:
                failures.append(f"{provider_name}: {exc}")
                self.logger.warning("Memory phrasing provider %s failed: %s", provider_name, exc)
        if failures:
            self.logger.warning("Memory phrasing fallback used: %s", "; ".join(failures))
        return self._heuristic_memory_preferences(text)

    def summarize_tool_reasoning(
        self,
        tool_name: str,
        phase: str,
        payload: dict,
        *,
        user_message: str = "",
        recent_summaries: list[str] | None = None,
    ) -> str:
        failures: list[str] = []
        request = {
            "tool_name": tool_name,
            "phase": phase,
            "payload": payload,
            "user_message": user_message,
            "recent_summaries": recent_summaries or [],
        }
        for provider_name, model in self._providers():
            try:
                if provider_name == "gemini":
                    return self._reasoning_with_gemini(request, model)
                return self._reasoning_with_openai(request, model)
            except ProviderError as exc:
                failures.append(f"{provider_name}: {exc}")
                self.logger.warning("Tool reasoning provider %s failed: %s", provider_name, exc)
        if failures:
            self.logger.warning("Tool reasoning fallback used: %s", "; ".join(failures))
        return self._heuristic_tool_reasoning(tool_name, phase, payload)

    def summarize_tool_result(
        self,
        tool_name: str,
        arguments: dict,
        raw_result: dict,
        *,
        user_message: str = "",
    ) -> str:
        failures: list[str] = []
        request = {
            "tool_name": tool_name,
            "arguments": arguments,
            "raw_result": raw_result,
            "user_message": user_message,
        }
        for provider_name, model in self._providers():
            try:
                if provider_name == "gemini":
                    return self._tool_result_summary_with_gemini(request, model)
                return self._tool_result_summary_with_openai(request, model)
            except ProviderError as exc:
                failures.append(f"{provider_name}: {exc}")
                self.logger.warning("Tool result summary provider %s failed: %s", provider_name, exc)
        if failures:
            self.logger.warning("Tool result summary fallback used: %s", "; ".join(failures))
        return self._heuristic_tool_result_summary(tool_name, raw_result)

    def plan_agent_run(
        self,
        user_message: str,
        history_messages: list[dict] | None = None,
    ) -> str | None:
        failures: list[str] = []
        request = {
            "user_message": user_message,
            "history_messages": history_messages or [],
        }
        for provider_name, model in self._providers():
            try:
                if provider_name == "gemini":
                    return self._plan_agent_run_with_gemini(request, model)
                return self._plan_agent_run_with_openai(request, model)
            except ProviderError as exc:
                failures.append(f"{provider_name}: {exc}")
                self.logger.warning("Run planning provider %s failed: %s", provider_name, exc)
        if failures:
            self.logger.warning("Run planning fallback used: %s", "; ".join(failures))
        return self._heuristic_agent_plan(user_message)

    def _providers(self) -> list[tuple[str, str]]:
        raw_override = (self.model_override or "").strip().lower()
        if raw_override == "auto":
            return [
                ("gemini", self.settings.ai.models.gemini),
                ("openai", self.settings.ai.models.openai),
            ]
        normalized_override = normalize_model_override(self.model_override)
        if normalized_override:
            if normalized_override == "gemini":
                return [("gemini", self.settings.ai.models.gemini)]
            if normalized_override == "openai":
                return [("openai", self.settings.ai.models.openai)]
            if "gemini" in normalized_override:
                return [("gemini", normalized_override)]
            return [("openai", normalized_override)]
        if self.settings.ai.provider == "gemini":
            return [("gemini", self.settings.ai.models.gemini)]
        if self.settings.ai.provider == "openai":
            return [("openai", self.settings.ai.models.openai)]
        return [
            ("gemini", self.settings.ai.models.gemini),
            ("openai", self.settings.ai.models.openai),
        ]

    def _route_prompt(self) -> str:
        suffix = f" {self.append_system_prompt}" if self.append_system_prompt else ""
        return (
            "You route forex-trading CLI requests. Return JSON only with keys: "
            "intent, scope, pair, sl_pips, risk_pct, lot_size, score_threshold, "
            "session_name, question, missing_fields. "
            "Valid intents: bias, scan, risk_size, risk_exposure, config_add_pair, "
            "config_remove_pair, config_show_pairs, config_show_risk, session_status, "
            "general_question, unknown. "
            "Use scope=all when the user asks about all pairs or market-wide context. "
            "Use pair aliases like Gold->XAUUSD, Euro->EURUSD, Cable/Pound->GBPUSD, Yen->USDJPY. "
            "Do not add prose outside JSON." + suffix
        )

    def _general_prompt(self) -> str:
        suffix = f" {self.append_system_prompt}" if self.append_system_prompt else ""
        return (
            "You are a concise forex trading CLI assistant. "
            "If live market context is provided, use it. If not, answer as general guidance and say it is not a live-data-backed view. "
            "Keep replies short and practical for a live session." + suffix
        )

    def _summary_prompt(self) -> str:
        return (
            "Summarize this trading conversation in 2 to 3 sentences. "
            "State what was discussed, which pairs or setups mattered, and what trade ideas or risks were surfaced. "
            "Keep it concise, factual, and useful as a resume recap."
        )

    def _memory_prompt(self) -> str:
        return (
            "Rewrite these raw PROPHET.md rules as a short second-person summary of the trader's preferences. "
            "Use 'you' and 'your', not 'I' or 'my'. "
            "Keep the meaning intact, do not invent details, and present it as natural language rather than raw bullets."
        )

    def _tool_reasoning_prompt(self) -> str:
        return (
            "You generate a live trading-assistant reasoning line for terminal streaming. "
            "Write exactly one short sentence in natural language. "
            "Base it only on the provided tool name, phase, arguments, and tool result payload. "
            "Do not mention JSON, payloads, IDs, or that you are an AI. "
            "For phase='before', describe what you are checking next. "
            "For phase='after', describe the most relevant finding or lack of finding from the returned result. "
            "Stay concise, specific, and trader-focused."
        )

    def _tool_result_summary_prompt(self) -> str:
        return (
            "Summarize this tool result in exactly one trader-focused sentence. "
            "Use only the provided tool name, arguments, and raw result. "
            "Do not mention JSON, payloads, or internal IDs."
        )

    def _research_plan_prompt(self) -> str:
        return (
            "Decide whether this trading-assistant request likely needs two or more tool calls before answering well. "
            "Return JSON only with keys needs_plan and steps. "
            "When needs_plan is true, steps must contain 2 to 5 short research steps. "
            "When needs_plan is false, steps must be an empty array. "
            "Use trader-friendly wording and keep each step short."
        )

    def _route_with_openai(self, message: str, context: dict, model: str) -> dict:
        if not self.env.openai_api_key:
            raise ProviderError("Missing OPENAI_API_KEY")
        try:
            response = self.openai_client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": self._route_prompt()},
                    {"role": "user", "content": json.dumps({"message": message, "context": context}, default=str)},
                ],
                reasoning={"effort": "minimal"},
                text={"format": {"type": "json_object"}},
            )
        except AuthenticationError as exc:
            raise ProviderError("OpenAI authentication failed") from exc
        except APITimeoutError as exc:
            raise ProviderError("OpenAI request timed out") from exc
        except APIConnectionError as exc:
            raise ProviderError("OpenAI connection failed") from exc
        except APIStatusError as exc:
            raise ProviderError(f"OpenAI returned HTTP {exc.status_code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"OpenAI request failed: {exc.__class__.__name__}") from exc
        if not response.output_text or not response.output_text.strip():
            raise ProviderError("OpenAI returned an empty response body")
        try:
            return json.loads(response.output_text)
        except ValueError as exc:
            raise ProviderError("OpenAI returned invalid JSON content") from exc

    def _answer_with_openai(self, message: str, context: dict, model: str) -> str:
        if not self.env.openai_api_key:
            raise ProviderError("Missing OPENAI_API_KEY")
        try:
            response = self.openai_client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": self._general_prompt()},
                    {"role": "user", "content": json.dumps({"message": message, "context": context}, default=str)},
                ],
                reasoning={"effort": "minimal"},
            )
        except AuthenticationError as exc:
            raise ProviderError("OpenAI authentication failed") from exc
        except APITimeoutError as exc:
            raise ProviderError("OpenAI request timed out") from exc
        except APIConnectionError as exc:
            raise ProviderError("OpenAI connection failed") from exc
        except APIStatusError as exc:
            raise ProviderError(f"OpenAI returned HTTP {exc.status_code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"OpenAI request failed: {exc.__class__.__name__}") from exc
        if not response.output_text or not response.output_text.strip():
            raise ProviderError("OpenAI returned an empty response body")
        return response.output_text.strip()

    def _summarize_with_openai(self, payload: dict, model: str) -> str:
        if not self.env.openai_api_key:
            raise ProviderError("Missing OPENAI_API_KEY")
        try:
            response = self.openai_client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": self._summary_prompt()},
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
                reasoning={"effort": "minimal"},
            )
        except AuthenticationError as exc:
            raise ProviderError("OpenAI authentication failed") from exc
        except APITimeoutError as exc:
            raise ProviderError("OpenAI request timed out") from exc
        except APIConnectionError as exc:
            raise ProviderError("OpenAI connection failed") from exc
        except APIStatusError as exc:
            raise ProviderError(f"OpenAI returned HTTP {exc.status_code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"OpenAI request failed: {exc.__class__.__name__}") from exc
        if not response.output_text or not response.output_text.strip():
            raise ProviderError("OpenAI returned an empty response body")
        return response.output_text.strip()

    def _describe_memory_with_openai(self, payload: dict, model: str) -> str:
        if not self.env.openai_api_key:
            raise ProviderError("Missing OPENAI_API_KEY")
        try:
            response = self.openai_client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": self._memory_prompt()},
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
                reasoning={"effort": "minimal"},
            )
        except AuthenticationError as exc:
            raise ProviderError("OpenAI authentication failed") from exc
        except APITimeoutError as exc:
            raise ProviderError("OpenAI request timed out") from exc
        except APIConnectionError as exc:
            raise ProviderError("OpenAI connection failed") from exc
        except APIStatusError as exc:
            raise ProviderError(f"OpenAI returned HTTP {exc.status_code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"OpenAI request failed: {exc.__class__.__name__}") from exc
        if not response.output_text or not response.output_text.strip():
            raise ProviderError("OpenAI returned an empty response body")
        return response.output_text.strip()

    def _reasoning_with_openai(self, payload: dict, model: str) -> str:
        if not self.env.openai_api_key:
            raise ProviderError("Missing OPENAI_API_KEY")
        try:
            response = self.openai_client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": self._tool_reasoning_prompt()},
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
                reasoning={"effort": "minimal"},
            )
        except AuthenticationError as exc:
            raise ProviderError("OpenAI authentication failed") from exc
        except APITimeoutError as exc:
            raise ProviderError("OpenAI request timed out") from exc
        except APIConnectionError as exc:
            raise ProviderError("OpenAI connection failed") from exc
        except APIStatusError as exc:
            raise ProviderError(f"OpenAI returned HTTP {exc.status_code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"OpenAI request failed: {exc.__class__.__name__}") from exc
        if not response.output_text or not response.output_text.strip():
            raise ProviderError("OpenAI returned an empty response body")
        return self._normalize_reasoning_line(response.output_text)

    def _tool_result_summary_with_openai(self, payload: dict, model: str) -> str:
        if not self.env.openai_api_key:
            raise ProviderError("Missing OPENAI_API_KEY")
        try:
            response = self.openai_client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": self._tool_result_summary_prompt()},
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
                reasoning={"effort": "minimal"},
            )
        except AuthenticationError as exc:
            raise ProviderError("OpenAI authentication failed") from exc
        except APITimeoutError as exc:
            raise ProviderError("OpenAI request timed out") from exc
        except APIConnectionError as exc:
            raise ProviderError("OpenAI connection failed") from exc
        except APIStatusError as exc:
            raise ProviderError(f"OpenAI returned HTTP {exc.status_code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"OpenAI request failed: {exc.__class__.__name__}") from exc
        if not response.output_text or not response.output_text.strip():
            raise ProviderError("OpenAI returned an empty response body")
        return self._normalize_reasoning_line(response.output_text)

    def _plan_agent_run_with_openai(self, payload: dict, model: str) -> str | None:
        if not self.env.openai_api_key:
            raise ProviderError("Missing OPENAI_API_KEY")
        try:
            response = self.openai_client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": self._research_plan_prompt()},
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
                reasoning={"effort": "minimal"},
                text={"format": {"type": "json_object"}},
            )
        except AuthenticationError as exc:
            raise ProviderError("OpenAI authentication failed") from exc
        except APITimeoutError as exc:
            raise ProviderError("OpenAI request timed out") from exc
        except APIConnectionError as exc:
            raise ProviderError("OpenAI connection failed") from exc
        except APIStatusError as exc:
            raise ProviderError(f"OpenAI returned HTTP {exc.status_code}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"OpenAI request failed: {exc.__class__.__name__}") from exc
        if not response.output_text or not response.output_text.strip():
            raise ProviderError("OpenAI returned an empty response body")
        try:
            parsed = json.loads(response.output_text)
        except ValueError as exc:
            raise ProviderError("OpenAI returned invalid JSON content") from exc
        return self._coerce_plan(parsed)

    def _route_with_gemini(self, message: str, context: dict, model: str) -> dict:
        if not self.env.gemini_api_key:
            raise ProviderError("Missing GEMINI_API_KEY")
        try:
            response = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": self.env.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": self._route_prompt()}]},
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": json.dumps({"message": message, "context": context}, default=str)}],
                        }
                    ],
                    "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
                },
                timeout=self.settings.chat.response_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Gemini request failed") from exc
        return self._parse_gemini_json(response)

    def _answer_with_gemini(self, message: str, context: dict, model: str) -> str:
        if not self.env.gemini_api_key:
            raise ProviderError("Missing GEMINI_API_KEY")
        try:
            response = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": self.env.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": self._general_prompt()}]},
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": json.dumps({"message": message, "context": context}, default=str)}],
                        }
                    ],
                    "generationConfig": {"temperature": 0.2},
                },
                timeout=self.settings.chat.response_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Gemini request failed") from exc
        if response.status_code != 200:
            raise ProviderError(f"Gemini returned HTTP {response.status_code}")
        try:
            return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Gemini returned invalid text content") from exc

    def _summarize_with_gemini(self, payload: dict, model: str) -> str:
        if not self.env.gemini_api_key:
            raise ProviderError("Missing GEMINI_API_KEY")
        try:
            response = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": self.env.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": self._summary_prompt()}]},
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": json.dumps(payload, default=str)}],
                        }
                    ],
                    "generationConfig": {"temperature": 0.1},
                },
                timeout=self.settings.chat.response_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Gemini request failed") from exc
        if response.status_code != 200:
            raise ProviderError(f"Gemini returned HTTP {response.status_code}")
        try:
            return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Gemini returned invalid text content") from exc

    def _describe_memory_with_gemini(self, payload: dict, model: str) -> str:
        if not self.env.gemini_api_key:
            raise ProviderError("Missing GEMINI_API_KEY")
        try:
            response = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": self.env.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": self._memory_prompt()}]},
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": json.dumps(payload, default=str)}],
                        }
                    ],
                    "generationConfig": {"temperature": 0.1},
                },
                timeout=self.settings.chat.response_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Gemini request failed") from exc
        if response.status_code != 200:
            raise ProviderError(f"Gemini returned HTTP {response.status_code}")
        try:
            return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Gemini returned invalid text content") from exc

    def _reasoning_with_gemini(self, payload: dict, model: str) -> str:
        if not self.env.gemini_api_key:
            raise ProviderError("Missing GEMINI_API_KEY")
        try:
            response = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": self.env.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": self._tool_reasoning_prompt()}]},
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": json.dumps(payload, default=str)}],
                        }
                    ],
                    "generationConfig": {"temperature": 0.2},
                },
                timeout=self.settings.chat.response_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Gemini request failed") from exc
        if response.status_code != 200:
            raise ProviderError(f"Gemini returned HTTP {response.status_code}")
        try:
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Gemini returned invalid text content") from exc
        return self._normalize_reasoning_line(text)

    def _tool_result_summary_with_gemini(self, payload: dict, model: str) -> str:
        if not self.env.gemini_api_key:
            raise ProviderError("Missing GEMINI_API_KEY")
        try:
            response = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": self.env.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": self._tool_result_summary_prompt()}]},
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": json.dumps(payload, default=str)}],
                        }
                    ],
                    "generationConfig": {"temperature": 0.1},
                },
                timeout=self.settings.chat.response_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Gemini request failed") from exc
        if response.status_code != 200:
            raise ProviderError(f"Gemini returned HTTP {response.status_code}")
        try:
            text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Gemini returned invalid text content") from exc
        return self._normalize_reasoning_line(text)

    def _plan_agent_run_with_gemini(self, payload: dict, model: str) -> str | None:
        if not self.env.gemini_api_key:
            raise ProviderError("Missing GEMINI_API_KEY")
        try:
            response = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": self.env.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": self._research_plan_prompt()}]},
                    "contents": [
                        {
                            "role": "user",
                            "parts": [{"text": json.dumps(payload, default=str)}],
                        }
                    ],
                    "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
                },
                timeout=self.settings.chat.response_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("Gemini request failed") from exc
        return self._coerce_plan(self._parse_gemini_json(response))

    def _parse_gemini_json(self, response: httpx.Response) -> dict:
        if response.status_code != 200:
            raise ProviderError(f"Gemini returned HTTP {response.status_code}")
        if not response.text.strip():
            raise ProviderError("Gemini returned an empty response body")
        try:
            raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            cleaned = self._coerce_json_text(raw_text)
            return json.loads(cleaned)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ProviderError("Gemini returned invalid JSON content") from exc

    def _coerce_json_text(self, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        first = stripped.find("{")
        last = stripped.rfind("}")
        if first != -1 and last != -1 and first < last:
            return stripped[first : last + 1]
        return stripped

    def _post_process(self, message: str, decision: RouteDecision, context: dict) -> RouteDecision:
        pair = normalize_pair_alias(decision.pair)
        if pair:
            decision.pair = pair
        elif context.get("active_pair") and decision.intent in {"bias", "scan", "risk_size", "risk_exposure"}:
            decision.pair = context["active_pair"]

        if decision.intent in {"bias", "scan"} and decision.scope is None:
            lowered = message.lower()
            if "all pair" in lowered or "all pairs" in lowered or "market" in lowered:
                decision.scope = "all"
            elif decision.pair:
                decision.scope = "single"

        if decision.intent == "risk_size":
            if decision.risk_pct is None:
                decision.risk_pct = self.settings.trading.risk.default_risk_pct
            decision.missing_fields = self._missing_fields(decision, ("pair", "sl_pips"))
        elif decision.intent == "risk_exposure":
            decision.missing_fields = self._missing_fields(decision, ("pair", "lot_size", "sl_pips"))
        elif decision.intent in {"config_add_pair", "config_remove_pair"}:
            decision.missing_fields = self._missing_fields(decision, ("pair",))
        elif decision.intent == "general_question" and not decision.question:
            decision.question = message
        return decision

    def _missing_fields(self, decision: RouteDecision, fields: Iterable[str]) -> list[str]:
        missing = []
        for field_name in fields:
            if getattr(decision, field_name) is None:
                missing.append(field_name)
        return missing

    def _heuristic_route(self, message: str, context: dict) -> RouteDecision:
        lowered = message.lower()
        pair = self._extract_pair(message) or context.get("active_pair")
        sl_pips = self._extract_int(r"(\d+)\s*pips?", lowered)
        risk_pct = self._extract_float(r"(\d+(?:\.\d+)?)\s*%", lowered)
        lot_size = self._extract_float(r"(\d+(?:\.\d+)?)\s*lots?", lowered)

        if ("watching" in lowered or "watchlist" in lowered) and any(word in lowered for word in ("what", "show")):
            return RouteDecision(intent="config_show_pairs")
        if "risk settings" in lowered:
            return RouteDecision(intent="config_show_risk")
        if lowered.startswith("add ") or "add " in lowered and "watch" in lowered:
            return RouteDecision(intent="config_add_pair", pair=pair)
        if lowered.startswith("remove ") or "remove " in lowered and ("watch" in lowered or "scan" in lowered):
            return RouteDecision(intent="config_remove_pair", pair=pair)
        if "session" in lowered or "london" in lowered or "new york" in lowered:
            session_name = "London" if "london" in lowered else "New York" if "new york" in lowered else None
            return RouteDecision(intent="session_status", session_name=session_name)
        if "lot size" in lowered or "position size" in lowered or "how many lots" in lowered:
            return RouteDecision(intent="risk_size", pair=pair, sl_pips=sl_pips, risk_pct=risk_pct)
        if "risk on" in lowered and lot_size is not None:
            return RouteDecision(intent="risk_exposure", pair=pair, sl_pips=sl_pips, lot_size=lot_size)
        if "setup" in lowered or "scan" in lowered or "worth trading" in lowered or "high probability" in lowered:
            score_threshold = 7 if "high probability" in lowered else None
            scope = "all" if "all" in lowered or "any" in lowered else "single" if pair else None
            return RouteDecision(intent="scan", pair=pair, scope=scope, score_threshold=score_threshold)
        if "bias" in lowered or "bullish" in lowered or "bearish" in lowered or "structure" in lowered:
            scope = "all" if "all" in lowered or "market" in lowered else "single" if pair else None
            return RouteDecision(intent="bias", pair=pair, scope=scope)
        if "fvg" in lowered or "explain" in lowered or "should i be trading" in lowered:
            return RouteDecision(intent="general_question", pair=pair, question=message)
        return RouteDecision(intent="unknown", question=message)

    def _heuristic_general_answer(self, message: str, context: dict) -> str:
        pair = context.get("active_pair")
        prefix = f"For {pair}, " if pair else ""
        return (
            f"{prefix}that’s general guidance rather than a live-data-backed view. "
            "Use the latest bias and setup scan before taking a trade."
        )

    def _heuristic_summary(self, turns: list[dict]) -> str:
        recent_turns = [turn for turn in turns[-5:] if turn.get("content")]
        user_turns = [turn.get("content", "").strip() for turn in recent_turns if turn.get("role") == "user"]
        assistant_turns = [turn.get("content", "").strip() for turn in recent_turns if turn.get("role") == "assistant"]
        if not user_turns and not assistant_turns:
            return "The session ended without any saved discussion."
        first = user_turns[0] if user_turns else "The trader asked for market guidance."
        pairs = sorted({
            pair
            for text in user_turns + assistant_turns
            for pair in ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD")
            if pair in text.upper()
        })
        setup_terms = [
            label
            for label in ("trend", "bias", "setup", "entry", "long", "short", "risk", "calendar")
            if any(label in text.lower() for text in user_turns + assistant_turns)
        ]
        pair_text = f" around {', '.join(pairs)}" if pairs else ""
        setup_text = f" with focus on {', '.join(setup_terms[:3])}" if setup_terms else ""
        summary = f"The session focused on {first}{pair_text}{setup_text}."
        summary = f"The session focused on {first}{pair_text}{setup_text}."
        if assistant_turns:
            last = assistant_turns[-1]
            cap = 200
            excerpt = last[:cap].rstrip() + ("…" if len(last) > cap else "")
            return f"{summary} Prophet surfaced {excerpt}"
        return summary
        return summary

    def _heuristic_memory_preferences(self, content: str) -> str:
        lines = []
        for raw_line in content.splitlines():
            text = raw_line.strip()
            if not text:
                continue
            if text.startswith("- "):
                text = text[2:].strip()
            lines.append(self._to_second_person(text))
        if not lines:
            return "Your trading preferences are not saved yet."
        if len(lines) == 1:
            return f"Your trading preference: {lines[0]}"
        return "Your trading preferences:\n- " + "\n- ".join(lines)

    def _heuristic_tool_reasoning(self, tool_name: str, phase: str, payload: dict) -> str:
        summary = str(payload.get("summary") or payload.get("recommendation") or "").strip()
        if phase == "before":
            pair = payload.get("pair")
            query = payload.get("query")
            if pair:
                return self._normalize_reasoning_line(f"Checking {pair} with {tool_name.replace('_', ' ')}.")
            if query:
                return self._normalize_reasoning_line(f"Looking into {query} with live search.")
            return self._normalize_reasoning_line(f"Running {tool_name.replace('_', ' ')} now.")

        if summary:
            return self._normalize_reasoning_line(summary)
        if payload.get("ok") is False:
            error = str(payload.get("error") or "the tool did not return a usable result").strip()
            return self._normalize_reasoning_line(f"That check hit an issue: {error}.")
        return self._normalize_reasoning_line(f"{tool_name.replace('_', ' ')} completed without a standout signal.")

    def _heuristic_tool_result_summary(self, tool_name: str, payload: dict) -> str:
        summary = str(payload.get("summary") or payload.get("recommendation") or "").strip()
        if summary:
            return self._normalize_reasoning_line(summary)
        if payload.get("ok") is False:
            error = str(payload.get("error") or "the tool returned an issue").strip()
            return self._normalize_reasoning_line(f"{tool_name.replace('_', ' ')} hit an issue: {error}.")
        return self._normalize_reasoning_line(f"{tool_name.replace('_', ' ')} completed without a standout signal.")

    def _heuristic_agent_plan(self, user_message: str) -> str | None:
        lowered = user_message.lower()
        signals = 0
        if any(term in lowered for term in ("news", "calendar", "event")):
            signals += 1
        if any(term in lowered for term in ("bias", "structure", "trend")):
            signals += 1
        if any(term in lowered for term in ("scan", "setup", "best setup", "compare", "rank")):
            signals += 1
        if any(term in lowered for term in ("risk", "lot size", "trade plan", "plan this trade")):
            signals += 1
        if signals < 2:
            return None
        return "\n".join(
            [
                "1. Confirm the live market context relevant to the request.",
                "2. Pull the strongest supporting signals from the right internal tools.",
                "3. Check whether risk, session timing, or news changes the trade decision.",
            ]
        )

    def _coerce_plan(self, payload: dict) -> str | None:
        needs_plan = bool(payload.get("needs_plan"))
        raw_steps = payload.get("steps") or []
        if not needs_plan:
            return None
        if not isinstance(raw_steps, list):
            return None
        steps = [" ".join(str(item).strip().split()) for item in raw_steps if str(item).strip()]
        if len(steps) < 2:
            return None
        return "\n".join(f"{index}. {step}" for index, step in enumerate(steps[:5], start=1))

    def _normalize_reasoning_line(self, text: str) -> str:
        value = " ".join(str(text or "").strip().split())
        if not value:
            return "Reviewing the latest tool result."
        if value[-1] not in ".!?":
            value += "."
        return value

    def _to_second_person(self, text: str) -> str:
        rewrites = (
            (r"^i\s+prefer\b", "You prefer"),
            (r"^i\s+like\b", "You like"),
            (r"^i\s+trade\b", "You trade"),
            (r"^i\s+only\b", "You only"),
            (r"^my\s+", "Your "),
            (r"^avoid\b", "You avoid"),
            (r"^never\b", "You never"),
            (r"^always\b", "You always"),
        )
        for pattern, replacement in rewrites:
            if re.match(pattern, text, flags=re.IGNORECASE):
                return re.sub(pattern, replacement, text, count=1, flags=re.IGNORECASE)
        if text.lower().startswith("you "):
            return text
        return f"You {text[0].lower() + text[1:]}" if text else text

    def _extract_pair(self, message: str) -> str | None:
        cleaned = re.sub(r"[^A-Za-z/ ]", " ", message)
        tokens = cleaned.split()
        for size in (2, 1):
            for idx in range(len(tokens) - size + 1):
                candidate = "".join(tokens[idx : idx + size])
                pair = normalize_pair_alias(candidate)
                if pair:
                    return pair
        return None

    def _extract_int(self, pattern: str, value: str) -> int | None:
        match = re.search(pattern, value)
        return int(match.group(1)) if match else None

    def _extract_float(self, pattern: str, value: str) -> float | None:
        match = re.search(pattern, value)
        return float(match.group(1)) if match else None
