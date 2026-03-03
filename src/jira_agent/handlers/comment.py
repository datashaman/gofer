import logging

from ..config import Settings
from ..dispatcher import handles
from ..models import JiraEvent

logger = logging.getLogger(__name__)


@handles("commented")
async def handle_comment(event: JiraEvent, settings: Settings) -> None:
    """Handle new comments on issues. Stub for Phase 3."""
    logger.info(
        "[comment] %s: %s — %s",
        event.event_type,
        event.issue_key,
        event.summary,
    )
