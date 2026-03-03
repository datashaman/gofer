import logging

from ..dispatcher import handles
from ..models import JiraEvent

logger = logging.getLogger(__name__)


@handles("commented")
async def handle_comment(event: JiraEvent) -> None:
    """Handle new comments on issues. Stub for Phase 2."""
    logger.info(
        "[comment] %s: %s — %s",
        event.event_type,
        event.issue_key,
        event.summary,
    )
