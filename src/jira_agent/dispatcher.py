from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from .config import Settings
from .models import EventType, JiraEvent

logger = logging.getLogger(__name__)

HandlerFunc = Callable[[JiraEvent, Settings], Coroutine[Any, Any, None]]

_handlers: dict[str, HandlerFunc] = {}


def handles(*event_types: EventType) -> Callable[[HandlerFunc], HandlerFunc]:
    """Decorator to register a handler for one or more event types."""

    def decorator(func: HandlerFunc) -> HandlerFunc:
        for et in event_types:
            if et in _handlers:
                logger.warning(
                    "Overwriting handler for %s: %s -> %s",
                    et,
                    _handlers[et].__name__,
                    func.__name__,
                )
            _handlers[et] = func
            logger.debug("Registered handler %s for event type %s", func.__name__, et)
        return func

    return decorator


async def dispatch(event: JiraEvent, settings: Settings) -> None:
    """Look up and call the handler for the given event."""
    handler = _handlers.get(event.event_type)
    if handler is None:
        logger.debug("No handler registered for event type: %s", event.event_type)
        return
    logger.info(
        "Dispatching %s event for %s to %s",
        event.event_type,
        event.issue_key,
        handler.__name__,
    )
    try:
        await handler(event, settings)
    except Exception:
        logger.exception(
            "Handler %s failed for %s %s",
            handler.__name__,
            event.event_type,
            event.issue_key,
        )
