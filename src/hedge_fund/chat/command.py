from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.prompt import Confirm

from hedge_fund.chat.agent_runtime import AgentEventSink, AgentRuntime
from hedge_fund.chat.cli_settings import CliSettings
from hedge_fund.chat.config_manager import ConfigManager
from hedge_fund.chat.ai import ChatLanguageService
from hedge_fund.chat.models import ChatResponse
from hedge_fund.chat.scratchpad import ScratchpadManager
from hedge_fund.chat.service import ChatService, ReverseRiskService
from hedge_fund.chat.session_store import SessionStore
from hedge_fund.chat.utils import current_session_status
from hedge_fund.cli.rendering import (
    agent_status,
    console,
    render_prophet_banner,
    render_biases,
    render_chat_message,
    render_chat_status,
    render_error,
    render_help_menu,
    render_model_picker,
    render_reverse_risk,
    render_risk,
    render_session_header,
    render_setups,
)
from hedge_fund.services.calendar_service import CalendarService
from hedge_fund.services.scan_service import RiskService, ScanService


class NullLanguageService:
    def __init__(self, settings) -> None:
        self.settings = settings

    def route(self, message: str, context: dict):
        raise RuntimeError("Legacy chat routing is unavailable in agent mode.")

    def answer_general(self, message: str, context: dict) -> str:
        raise RuntimeError("Legacy chat routing is unavailable in agent mode.")


class RichAgentEventSink(AgentEventSink):
    def __init__(self, status, show_thinking: bool) -> None:
        self.status = status
        self.show_thinking = show_thinking

    def update_status(self, message: str) -> None:
        self.status.update(f"[bold cyan]{message}[/bold cyan]")

    def emit_plan(self, message: str) -> None:
        if self.show_thinking:
            console.log(f"[dim]{message}[/dim]")

    def emit_reasoning(self, message: str) -> None:
        if self.show_thinking:
            console.log(f"[dim]{message}[/dim]")


class ChatCommandRunner:
    def __init__(self, context, cwd: str | Path | None = None, session_store=None, repository=None) -> None:
        self.context = context
        self.cwd = Path(cwd or Path.cwd())
        self.cli_settings = CliSettings.load(self.cwd)
        self.session_store = session_store or SessionStore(self.cwd)
        self._repository_session = None
        if repository is None:
            self._repository_session = self.context.create_session()
            self.repository = self.context.create_repository(self._repository_session)
        else:
            self.repository = repository

    def run(
        self,
        prompt: str | None,
        print_mode: bool,
        continue_last: bool,
        resume_session: str | None,
        output_format: str | None,
        model_override: str | None,
        permission_mode: str | None,
        append_system_prompt: str | None,
    ) -> None:
        if continue_last and resume_session:
            raise typer.BadParameter("Use either --continue or --resume, not both.")
        if print_mode and not prompt:
            raise typer.BadParameter("Print mode requires a prompt.")

        effective_output = output_format or self.cli_settings.output_format or "text"
        effective_model = model_override or self.cli_settings.model
        effective_permission = permission_mode or self.cli_settings.permission_mode or "default"
        effective_prompt = append_system_prompt or self.cli_settings.append_system_prompt
        if effective_output not in {"text", "json"}:
            raise typer.BadParameter("Output format must be text or json.")
        if effective_permission not in {"default", "plan", "accept_edits"}:
            raise typer.BadParameter("Permission mode must be default, plan, or accept_edits.")

        state = self._load_state(continue_last, resume_session, effective_permission, effective_model, effective_prompt)
        service = self.build_service(effective_model, effective_prompt)

        should_render_intro = not print_mode and effective_output == "text"
        if should_render_intro:
            self._render_session_intro()
            if continue_last or resume_session:
                self._render_resume_recap(state)

        if prompt:
            response = self._process_with_optional_status(
                service,
                state,
                prompt,
                effective_output,
                print_mode,
            )
            self._render_response(response, effective_output, print_mode)
            if print_mode:
                return

        self._interactive_loop(state, service, show_intro=not should_render_intro)

    def _load_state(
        self,
        continue_last: bool,
        resume_session: str | None,
        permission_mode: str,
        model_override: str | None,
        append_system_prompt: str | None,
    ):
        if continue_last:
            return self.session_store.load_latest()
        if resume_session:
            return self.session_store.load(resume_session)
        return self.session_store.create(
            max_context_turns=self.context.settings.chat.max_context_turns,
            permission_mode=permission_mode,
            model_override=model_override,
            append_system_prompt=append_system_prompt,
        )

    def build_service(
        self,
        model_override: str | None,
        append_system_prompt: str | None,
        device_token: str | None = None,
    ) -> ChatService:
        language = ChatLanguageService(
            self.context.settings,
            self.context.env,
            self.context.logger,
            model_override=model_override,
            append_system_prompt=append_system_prompt,
        )
        scan_service = ScanService(
            self.context.settings,
            self.context.market_data,
            self.context.ai,
            self.repository,
            self.context.logger,
        )
        risk_service = RiskService(self.context.market_data, self.context.broker)
        reverse_risk_service = ReverseRiskService(self.context.market_data, self.context.broker)
        config_manager = ConfigManager(self.cwd / "config.yaml")
        scratchpad_manager = ScratchpadManager(self.cwd, self.context.settings.agent)
        calendar_service = CalendarService(self.context.calendar)
        agent_runtime = AgentRuntime(
            self.context.settings,
            self.context.env,
            self.context.logger,
            model_override=model_override,
        )
        return ChatService(
            self.context.settings,
            scan_service,
            risk_service,
            reverse_risk_service,
            language,
            config_manager,
            self.session_store,
            agent_runtime=agent_runtime,
            scratchpad_manager=scratchpad_manager,
            search_client=getattr(self.context, "web_search", None),
            memory_repository=self.context.create_memory_repository(self.repository.session, device_token=device_token),
            user_profile_repository=self.context.create_user_profile_repository(self.repository.session) if device_token else None,
            calendar_service=calendar_service,
        )

    def close(self) -> None:
        if self._repository_session is not None:
            self._repository_session.close()
            self._repository_session = None

    def _interactive_loop(self, state, service: ChatService, show_intro: bool) -> None:
        if show_intro:
            self._render_session_intro()
        render_chat_status(f"Chat session {state.session.session_id}. Type /help for commands. Type exit or quit to leave.")
        while True:
            try:
                message = console.input("[bold cyan]> [/bold cyan]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if message.lower() in {"exit", "quit"}:
                break
            response = self._process_with_optional_status(service, state, message, "text", False)
            self._render_response(response, "text", False)
            if response.should_exit:
                break

    def _process_with_optional_status(
        self,
        service: ChatService,
        state,
        message: str,
        output_format: str,
        print_mode: bool,
    ) -> ChatResponse:
        authorize = self._confirm if not print_mode else None
        if print_mode or output_format != "text" or message.startswith("/"):
            return service.process_message(state, message, authorize)

        with agent_status("Thinking...") as status:
            sink = RichAgentEventSink(status, self.context.settings.agent.show_thinking)
            return service.process_message(state, message, authorize, event_sink=sink)

    def _render_response(self, response: ChatResponse, output_format: str, print_mode: bool) -> None:
        if output_format == "json":
            typer.echo(json.dumps(response.model_dump(mode="json", exclude_none=True), indent=2))
            return

        if response.message:
            view = response.metadata.get("view")
            if view == "help_menu":
                render_chat_status(response.message)
                render_help_menu(response.metadata.get("commands", []))
            elif view == "model_picker":
                render_chat_status(response.message)
                render_model_picker(response.metadata.get("current", ""), response.metadata.get("options", []))
            else:
                render_chat_message(response.message)
        if response.biases:
            render_biases(response.biases)
        if response.setups:
            render_setups(response.setups)
        if response.risk:
            render_risk(response.risk)
        if response.reverse_risk:
            render_reverse_risk(response.reverse_risk)
        if response.ai_analysis and not print_mode:
            from hedge_fund.cli.rendering import render_ai_output

            render_ai_output(response.ai_analysis)

    def _confirm(self, question: str) -> bool:
        try:
            return Confirm.ask(question, default=False)
        except Exception:  # noqa: BLE001
            render_error("Could not confirm config change.")
            return False

    def _render_session_intro(self) -> None:
        render_prophet_banner()
        config_path = self.cwd / "config.yaml"
        pairs = ", ".join(ConfigManager(config_path).show_pairs() if config_path.exists() else self.context.settings.trading.pairs)
        session = current_session_status(self.context.settings.trading.sessions)
        if session["current_session"] == "Closed":
            time_until = session.get("time_until_open")
            if time_until:
                header = f"Session: Market Closed  |  Opens in {time_until}  |  Type /help for commands"
            else:
                header = f"Session: Market Closed  |  Opens at {session['opens_at']} UTC  |  Type /help for commands"
        else:
            header = f"Session: {session['current_session']} Open  |  Pairs: {pairs}  |  Type /help for commands"
        render_session_header(header)

    def _render_resume_recap(self, state) -> None:
        if state.session.summary:
            render_chat_status(state.session.summary)
            return
        summarize = getattr(self.build_service(state.session.model_override, state.session.append_system_prompt).language, "summarize_session", None)
        if callable(summarize):
            turns = [
                {"role": turn.role, "content": turn.content, "metadata": turn.metadata}
                for turn in state.session.turns[-5:]
            ]
            summary = summarize(turns)
            if summary:
                render_chat_status(summary)
