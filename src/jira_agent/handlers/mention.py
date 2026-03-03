import logging

from ..config import Settings
from ..dispatcher import handles
from ..models import JiraEvent

logger = logging.getLogger(__name__)


@handles("mentioned")
async def handle_mention(event: JiraEvent, settings: Settings) -> None:
    """Handle mentions in issues/comments. Stub for Phase 3."""
    logger.info(
        "[mention] %s: %s — %s",
        event.event_type,
        event.issue_key,
        event.summary,
    )
