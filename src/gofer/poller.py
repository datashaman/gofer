from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import Settings
from .events import classify_changes
from .jira_client import get_jira_client
from .models import JiraEvent

logger = logging.getLogger(__name__)


class JiraPoller:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Maps issue key -> last seen raw issue dict
        self._state: dict[str, dict[str, Any]] = {}
        self._my_email = settings.env.jira_email

    async def poll(self) -> list[JiraEvent]:
        """Poll Jira for updated issues and return classified events."""
        interval = self._settings.config.poll_interval
        jql = (
            f'assignee = currentUser() AND updated >= "-{interval}m" '
            f"ORDER BY updated DESC"
        )
        logger.debug("Polling with JQL: %s", jql)

        # jira library is synchronous, run in thread pool
        client = get_jira_client()
        loop = asyncio.get_running_loop()
        issues = await loop.run_in_executor(
            None,
            lambda: client.search_issues(jql, maxResults=50),
        )

        all_events: list[JiraEvent] = []
        for issue in issues:
            key = issue.key
            current = issue.raw
            previous = self._state.get(key)

            events = classify_changes(current, previous, self._my_email)
            all_events.extend(events)

            # Update stored state
            self._state[key] = current

        if all_events:
            logger.info("Poll returned %d events from %d issues", len(all_events), len(issues))
        else:
            logger.debug("Poll returned 0 events from %d issues", len(issues))

        return all_events
