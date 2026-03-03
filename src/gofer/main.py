from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from types import FrameType

from pydantic import ValidationError

from .approval import set_decision
from .batch import fetch_tickets, run_batch
from .config import load_settings
from .dispatcher import dispatch
from .events import InvalidIssueKey, build_event_from_issue, validate_issue_key
from .jira_client import init_jira_client
from .poller import JiraPoller
from .session import init_session_manager

# Import handlers to register them via @handles decorators
from . import handlers  # noqa: F401

logger = logging.getLogger("gofer")

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
        "Starting gofer: polling %s every %ds",
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


async def run_do(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)

    if args.max_parallel is not None:
        settings.config.concurrency.max_parallel_sessions = args.max_parallel

    # Build JQL
    if args.jql:
        jql = args.jql
    elif args.project:
        jql = (
            f'assignee = currentUser() AND project = "{args.project}" '
            f"AND statusCategory != Done ORDER BY priority DESC"
        )
    else:
        print("Error: provide a project key or --jql", file=sys.stderr)
        sys.exit(1)

    # Initialize singletons
    init_jira_client(settings)
    init_session_manager(settings)

    logger.info("Fetching tickets: %s", jql)
    issues = await fetch_tickets(jql)

    if not issues:
        print("No tickets found.")
        return

    events = [build_event_from_issue(issue, "assigned_to_me") for issue in issues]
    print(f"Found {len(events)} ticket(s):")
    for event in events:
        print(f"  {event.issue_key}: {event.summary}")

    if args.dry_run:
        return

    results = await run_batch(events, settings)

    succeeded = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    print(f"\nDone: {succeeded} succeeded, {failed} failed")
    for r in results:
        if not r.success:
            print(f"  FAILED {r.issue_key}: {r.error}")


def _setup_logging(args: argparse.Namespace) -> None:
    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_format = "%(asctime)s %(process)d %(levelname)-8s %(name)s: %(message)s"
    log_handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        log_handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=log_handlers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gofer",
        description="Polls Jira for ticket events and dispatches handlers.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
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

    subparsers = parser.add_subparsers(dest="command")

    # Default daemon mode (no subcommand)
    run_parser = subparsers.add_parser("run", help="Start the polling daemon")
    run_parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override poll interval in seconds",
    )

    # Approve subcommand
    approve_parser = subparsers.add_parser("approve", help="Approve a pending ticket")
    approve_parser.add_argument("issue_key", help="Jira issue key (e.g. PROJ-123)")

    # Reject subcommand
    reject_parser = subparsers.add_parser("reject", help="Reject a pending ticket")
    reject_parser.add_argument("issue_key", help="Jira issue key (e.g. PROJ-123)")

    # Do subcommand — batch work tickets
    do_parser = subparsers.add_parser("do", help="Batch work your open tickets")
    do_parser.add_argument(
        "project",
        nargs="?",
        default=None,
        help="Jira project key (e.g. PROJ)",
    )
    do_parser.add_argument(
        "--jql",
        default=None,
        help="Custom JQL query (overrides project-based query)",
    )
    do_parser.add_argument(
        "--max-parallel",
        type=int,
        default=None,
        help="Override max parallel sessions",
    )
    do_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching tickets without working them",
    )

    args = parser.parse_args()
    _setup_logging(args)

    try:
        if args.command in ("approve", "reject"):
            try:
                validate_issue_key(args.issue_key)
            except InvalidIssueKey as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

        if args.command == "approve":
            settings = load_settings(args.config)
            if set_decision(args.issue_key, "approved", settings):
                print(f"Approved {args.issue_key}")
            else:
                print(f"No pending approval found for {args.issue_key}", file=sys.stderr)
                sys.exit(1)

        elif args.command == "reject":
            settings = load_settings(args.config)
            if set_decision(args.issue_key, "rejected", settings):
                print(f"Rejected {args.issue_key}")
            else:
                print(f"No pending approval found for {args.issue_key}", file=sys.stderr)
                sys.exit(1)

        elif args.command == "do":
            asyncio.run(run_do(args))

        else:
            # Default: run the daemon (either no subcommand or "run")
            if not hasattr(args, "interval"):
                args.interval = None

            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
            asyncio.run(run_loop(args))

    except ValidationError as e:
        logger.error("Configuration error — check .env and config.yaml:")
        for err in e.errors():
            field = ".".join(str(loc) for loc in err["loc"])
            logger.error("  %s: %s", field, err["msg"])
        sys.exit(1)
