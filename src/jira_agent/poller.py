from __future__ import annotations

import asyncio
import logging
from typing import Any

from jira import JIRA

from .config import Settings
from .events import classify_changes
from .models import JiraEvent

logger = logging.getLogger(__name__)


class JiraPoller:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = JIRA(
            server=settings.env.jira_url,
            basic_auth=(settings.env.jira_email, settings.env.jira_api_token),
        )
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
        loop = asyncio.get_event_loop()
        issues = await loop.run_in_executor(
            None,
            lambda: self._client.search_issues(jql, maxResults=50, expand="changelog"),
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
