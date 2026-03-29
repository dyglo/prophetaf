from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hedge_fund.config.settings import AgentConfig


class ScratchpadLogger:
    def __init__(self, root: Path, session_id: str, enabled: bool) -> None:
        self.enabled = enabled
        self.path = root / f"{session_id}.jsonl"
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_written = self.path.exists() and self.path.stat().st_size > 0
        self.session_id = session_id

    def ensure_init(self, query: str, plan: str | None) -> None:
        if not self.enabled:
            return
        if self._init_written:
            return
        self._write(
            {
                "type": "init",
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "sessionId": self.session_id,
                "query": query,
                "plan": plan,
            }
        )
        self._init_written = True

    def log_thinking(self, message: str, details: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        payload: dict[str, Any] = {
            "type": "thinking",
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "message": message,
        }
        if details:
            payload["details"] = details
        self._write(payload)

    def log_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        raw_result: Any,
        llm_summary: str,
    ) -> None:
        if not self.enabled:
            return
        self._write(
            {
                "type": "tool_result",
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "toolName": tool_name,
                "args": arguments,
                "rawResult": raw_result,
                "llmSummary": llm_summary,
            }
        )

    def _write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, default=str) + "\n")


class ScratchpadManager:
    def __init__(self, cwd: str | Path, config: AgentConfig) -> None:
        self.root = Path(cwd) / config.scratchpad_path
        self.enabled = config.scratchpad_enabled

    def for_session(self, session_id: str) -> ScratchpadLogger:
        return ScratchpadLogger(self.root, session_id, self.enabled)
