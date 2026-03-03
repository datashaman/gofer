from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)


@dataclass
class SessionResult:
    issue_key: str
    success: bool
    cost_usd: float | None = None
    duration_ms: int = 0
    num_turns: int = 0
    session_id: str | None = None
    error: str | None = None
    response_text: str | None = None


class SessionManager:
    """Manages concurrent Claude Code sessions with semaphore-based throttling."""

    def __init__(self, max_parallel: int = 3, session_timeout: int = 3600) -> None:
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._session_timeout = session_timeout
        self._active: dict[str, asyncio.Task[SessionResult]] = {}
        self._max_parallel = max_parallel
        logger.info(
            "SessionManager initialized: max_parallel=%d, timeout=%ds",
            max_parallel,
            session_timeout,
        )

    def is_active(self, issue_key: str) -> bool:
        """Check if a session is currently running for the given issue."""
        task = self._active.get(issue_key)
        return task is not None and not task.done()

    async def run_session(
        self,
        *,
        issue_key: str,
        prompt: str,
        cwd: str | Path,
        system_prompt: str | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        env: dict[str, str] | None = None,
        permission_mode: str = "bypassPermissions",
        disallowed_tools: list[str] | None = None,
    ) -> SessionResult:
        """Run a Claude Code session, blocking on the semaphore for concurrency control."""
        if self.is_active(issue_key):
            logger.warning("Session already active for %s — skipping", issue_key)
            return SessionResult(
                issue_key=issue_key,
                success=False,
                error="Session already active",
            )

        async with self._semaphore:
            logger.info("Acquired semaphore for %s (%d slots)", issue_key, self._max_parallel)
            task = asyncio.current_task()
            if task is not None:
                self._active[issue_key] = task
            try:
                result = await asyncio.wait_for(
                    self._execute_session(
                        issue_key=issue_key,
                        prompt=prompt,
                        cwd=cwd,
                        system_prompt=system_prompt,
                        model=model,
                        max_turns=max_turns,
                        env=env or {},
                        permission_mode=permission_mode,
                        disallowed_tools=disallowed_tools,
                    ),
                    timeout=self._session_timeout,
                )
            except asyncio.TimeoutError:
                logger.error("Session for %s timed out after %ds", issue_key, self._session_timeout)
                result = SessionResult(
                    issue_key=issue_key,
                    success=False,
                    error=f"Session timed out after {self._session_timeout}s",
                )
            except asyncio.CancelledError:
                logger.info("Session for %s was cancelled", issue_key)
                result = SessionResult(
                    issue_key=issue_key,
                    success=False,
                    error="Session cancelled",
                )
            except Exception as exc:
                logger.exception("Session for %s failed with unexpected error", issue_key)
                result = SessionResult(
                    issue_key=issue_key,
                    success=False,
                    error=str(exc),
                )
            finally:
                self._active.pop(issue_key, None)

            return result

    async def _execute_session(
        self,
        *,
        issue_key: str,
        prompt: str,
        cwd: str | Path,
        system_prompt: str | None,
        model: str | None,
        max_turns: int | None,
        env: dict[str, str],
        permission_mode: str = "bypassPermissions",
        disallowed_tools: list[str] | None = None,
    ) -> SessionResult:
        """Build options, stream query(), and collect results."""
        ticket_logger = logging.getLogger(f"gofer.session.{issue_key}")
        start = time.monotonic()

        options = ClaudeCodeOptions(
            model=model,
            max_turns=max_turns,
            cwd=str(cwd),
            system_prompt=system_prompt,
            permission_mode=permission_mode,
            disallowed_tools=disallowed_tools or [],
            env=env,
        )

        ticket_logger.info("Starting Claude Code session (model=%s, max_turns=%s)", model, max_turns)

        result = SessionResult(issue_key=issue_key, success=False)
        last_assistant_text: str | None = None

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                # Collect text blocks from each assistant message; keep the latest
                text_parts = [
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                ]
                if text_parts:
                    last_assistant_text = "\n".join(text_parts)
                for block in message.content:
                    if isinstance(block, TextBlock):
                        ticket_logger.info("%s", block.text)
                    elif isinstance(block, ToolUseBlock):
                        ticket_logger.debug("Tool call: %s(%s)", block.name, _truncate(str(block.input)))
                    elif isinstance(block, ToolResultBlock):
                        ticket_logger.debug("Tool result for %s: %s", block.tool_use_id, _truncate(str(block.content)))
            elif isinstance(message, ResultMessage):
                elapsed_ms = int((time.monotonic() - start) * 1000)
                # Use last assistant text, falling back to result text on success
                response_text = last_assistant_text
                if response_text is None and not message.is_error and message.result:
                    response_text = message.result
                result = SessionResult(
                    issue_key=issue_key,
                    success=not message.is_error,
                    cost_usd=message.total_cost_usd,
                    duration_ms=elapsed_ms,
                    num_turns=message.num_turns,
                    session_id=message.session_id,
                    error=message.result if message.is_error else None,
                    response_text=response_text,
                )
                ticket_logger.info(
                    "Session complete: success=%s, turns=%d, cost=$%.4f, duration=%dms",
                    result.success,
                    result.num_turns,
                    result.cost_usd or 0,
                    result.duration_ms,
                )

        return result

    async def cancel_all(self) -> None:
        """Cancel all active sessions. Called during shutdown."""
        if not self._active:
            return
        logger.info("Cancelling %d active session(s)", len(self._active))
        for issue_key, task in self._active.items():
            if not task.done():
                logger.info("Cancelling session for %s", issue_key)
                task.cancel()
        # Give tasks a moment to handle cancellation
        if self._active:
            await asyncio.gather(*self._active.values(), return_exceptions=True)
        self._active.clear()


def _truncate(s: str, max_len: int = 200) -> str:
    return s[:max_len] + "..." if len(s) > max_len else s


# ---------------------------------------------------------------------------
# Shared singleton
# ---------------------------------------------------------------------------

_session_manager: SessionManager | None = None


def init_session_manager(settings: Settings) -> SessionManager:
    """Create the module-level session manager. Called once from main.py."""
    global _session_manager
    concurrency = settings.config.concurrency
    _session_manager = SessionManager(
        max_parallel=concurrency.max_parallel_sessions,
        session_timeout=concurrency.session_timeout,
    )
    return _session_manager


def get_session_manager() -> SessionManager | None:
    """Return the session manager singleton (None if not yet initialized)."""
    return _session_manager
