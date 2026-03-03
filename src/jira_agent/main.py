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
from .jira_client import init_jira_client
from .poller import JiraPoller
from .session import init_session_manager

# Import handlers to register them via @handles decorators
from . import handlers  # noqa: F401

logger = logging.getLogger("jira_agent")

_shutdown: asyncio.Event | None = None


def _handle_signal(sig: int, _frame: FrameType | None) -> None:
    logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
    if _shutdown is not None:
        _shutdown.set()


async def run_loop(settings_args: argparse.Namespace) -> None:
    global _shutdown
    _shutdown = asyncio.Event()

    settings = load_settings(settings_args.config)

    if settings_args.interval:
        settings.config.poll_interval = settings_args.interval

    # Initialize shared singletons before starting poll loop
    init_jira_client(settings)
    session_mgr = init_session_manager(settings)
    logger.info("Max parallel sessions: %d", settings.config.concurrency.max_parallel_sessions)

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
                await dispatch(event, settings)
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("Error during poll cycle")

        # Sleep in small increments so we can respond to shutdown quickly
        for _ in range(interval):
            if _shutdown.is_set():
                break
            await asyncio.sleep(1)

    await session_mgr.cancel_all()
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
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to log file (in addition to stderr)",
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_format = "%(asctime)s %(process)d %(levelname)-8s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=handlers,
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
