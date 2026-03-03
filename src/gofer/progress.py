from __future__ import annotations

import sys
import time
from typing import Literal

from rich.live import Live
from rich.table import Table

from .models import JiraEvent

Stage = Literal[
    "queued",
    "resolving",
    "gating",
    "waiting_approval",
    "working",
    "done",
    "failed",
    "skipped",
]

_STAGE_STYLES: dict[Stage, str] = {
    "queued": "dim",
    "resolving": "cyan",
    "gating": "yellow",
    "waiting_approval": "bold yellow",
    "working": "bold blue",
    "done": "bold green",
    "failed": "bold red",
    "skipped": "dim",
}

_TERMINAL_STAGES: set[Stage] = {"done", "failed", "skipped"}


class _TicketState:
    __slots__ = ("issue_key", "summary", "stage", "detail", "start_time")

    def __init__(self, issue_key: str, summary: str) -> None:
        self.issue_key = issue_key
        self.summary = summary
        self.stage: Stage = "queued"
        self.detail = ""
        self.start_time = time.monotonic()


class ProgressTracker:
    """Live progress display for batch ticket work.

    TTY mode: rich Live table with per-ticket rows.
    Non-TTY mode: plain status lines to stderr on each transition.
    """

    def __init__(self, events: list[JiraEvent], *, use_rich: bool = True) -> None:
        self._use_rich = use_rich
        self._tickets: dict[str, _TicketState] = {}
        self._start_time = time.monotonic()
        self._live: Live | None = None

        for event in events:
            summary = event.summary
            if len(summary) > 50:
                summary = summary[:47] + "..."
            self._tickets[event.issue_key] = _TicketState(event.issue_key, summary)

    def update(self, issue_key: str, stage: Stage, detail: str = "") -> None:
        """Update a ticket's current stage."""
        state = self._tickets.get(issue_key)
        if state is None:
            return

        state.stage = stage
        state.detail = detail

        if self._use_rich and self._live is not None:
            self._live.update(self._build_table())
        elif not self._use_rich:
            print(
                f"[{stage.upper()}] {issue_key}: {detail}" if detail else f"[{stage.upper()}] {issue_key}",
                file=sys.stderr,
            )

    def _build_table(self) -> Table:
        table = Table(title="gofer do", expand=True)
        table.add_column("Ticket", style="bold", no_wrap=True)
        table.add_column("Summary", ratio=2)
        table.add_column("Stage", no_wrap=True)
        table.add_column("Elapsed", no_wrap=True, justify="right")
        table.add_column("Detail", ratio=1)

        now = time.monotonic()
        for state in self._tickets.values():
            elapsed = now - state.start_time
            minutes, seconds = divmod(int(elapsed), 60)
            elapsed_str = f"{minutes}:{seconds:02d}"

            style = _STAGE_STYLES.get(state.stage, "")
            table.add_row(
                state.issue_key,
                state.summary,
                f"[{style}]{state.stage}[/{style}]",
                elapsed_str,
                state.detail,
            )

        # Summary line
        total = len(self._tickets)
        done = sum(1 for s in self._tickets.values() if s.stage in _TERMINAL_STAGES)
        table.caption = f"{done}/{total} complete"

        return table

    async def __aenter__(self) -> ProgressTracker:
        if self._use_rich:
            self._live = Live(self._build_table(), refresh_per_second=2)
            self._live.__enter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._live is not None:
            # Final update so the table shows end state
            self._live.update(self._build_table())
            self._live.__exit__(None, None, None)
            self._live = None
