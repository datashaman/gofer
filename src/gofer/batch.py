from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .handlers.ticket_work import handle_ticket_work
from .jira_client import get_jira_client
from .models import JiraEvent

logger = logging.getLogger(__name__)


@dataclass
class TicketResult:
    issue_key: str
    success: bool
    error: str | None = None


async def fetch_tickets(jql: str) -> list[dict[str, Any]]:
    """Run a JQL query and return raw issue dicts."""
    client = get_jira_client()
    loop = asyncio.get_running_loop()
    issues = await loop.run_in_executor(
        None,
        lambda: client.search_issues(jql, maxResults=50),
    )
    return [issue.raw for issue in issues]


async def run_batch(
    events: list[JiraEvent], settings: Settings
) -> list[TicketResult]:
    """Fire all events through handle_ticket_work concurrently.

    The SessionManager semaphore handles throttling, so all tasks are
    gathered at once and queued by the semaphore.
    """

    async def _work(event: JiraEvent) -> TicketResult:
        try:
            await handle_ticket_work(event, settings)
            return TicketResult(issue_key=event.issue_key, success=True)
        except Exception as exc:
            logger.exception("Batch work failed for %s", event.issue_key)
            return TicketResult(
                issue_key=event.issue_key, success=False, error=str(exc)
            )

    results = await asyncio.gather(*(_work(e) for e in events))
    return list(results)
