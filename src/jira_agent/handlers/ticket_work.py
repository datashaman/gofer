import logging

from ..dispatcher import handles
from ..models import JiraEvent

logger = logging.getLogger(__name__)


@handles("assigned_to_me", "status_changed")
async def handle_ticket_work(event: JiraEvent) -> None:
    """Handle ticket assignment and status changes. Stub for Phase 2."""
    logger.info(
        "[ticket_work] %s: %s — %s (%s)",
        event.event_type,
        event.issue_key,
        event.summary,
        event.status,
    )
