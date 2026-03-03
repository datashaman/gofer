from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from types import FrameType

from pydantic import ValidationError

from .config import load_settings
from .dispatcher import dispatch
from .poller import JiraPoller

# Import handlers to register them via @handles decorators
from . import handlers  # noqa: F401

logger = logging.getLogger("jira_agent")

_shutdown = asyncio.Event()


def _handle_signal(sig: int, _frame: FrameType | None) -> None:
    logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
    _shutdown.set()


async def run_loop(settings_args: argparse.Namespace) -> None:
    settings = load_settings(settings_args.config)

    if settings_args.interval:
        settings.config.poll_interval = settings_args.interval

    poller = JiraPoller(settings)
    interval = settings.config.poll_interval

    logger.info(
        "Starting jira-agent: polling %s every %ds",
        settings.env.jira_url,
        interval,
    )

    while not _shutdown.is_set():
        try:
            events = await poller.poll()
            for event in events:
                await dispatch(event)
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("Error during poll cycle")

        # Sleep in small increments so we can respond to shutdown quickly
        for _ in range(interval):
            if _shutdown.is_set():
                break
            await asyncio.sleep(1)

    logger.info("Shutdown complete.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jira-agent",
        description="Polls Jira for ticket events and dispatches handlers.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override poll interval in seconds",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        asyncio.run(run_loop(args))
    except ValidationError as e:
        logger.error("Configuration error — check .env and config.yaml:")
        for err in e.errors():
            field = ".".join(str(loc) for loc in err["loc"])
            logger.error("  %s: %s", field, err["msg"])
        sys.exit(1)
