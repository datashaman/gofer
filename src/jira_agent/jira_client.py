from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

from jira import JIRA

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)

_client: JIRA | None = None


def init_jira_client(settings: Settings) -> JIRA:
    """Create the module-level JIRA client singleton. Called once from main.py."""
    global _client
    _client = JIRA(
        server=settings.env.jira_url,
        basic_auth=(settings.env.jira_email, settings.env.jira_api_token),
    )
    logger.info("Jira client initialized for %s", settings.env.jira_url)
    return _client


def get_jira_client() -> JIRA:
    """Return the Jira client singleton; raises if not initialized."""
    if _client is None:
        raise RuntimeError("Jira client not initialized — call init_jira_client() first")
    return _client


async def add_comment(issue_key: str, body: str) -> None:
    """Post a comment to a Jira issue (async wrapper around the sync client)."""
    client = get_jira_client()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(client.add_comment, issue_key, body))
    logger.info("Posted comment to %s (%d chars)", issue_key, len(body))
