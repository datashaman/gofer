from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from types import FrameType

from pydantic import ValidationError

from .approval import _FRESH_SENTINEL, get_pending_branches, set_branch_selection, set_decision
from .batch import fetch_tickets, run_batch
from .config import load_settings, save_active_branch
from .dispatcher import dispatch
from .events import InvalidIssueKey, build_event_from_issue, validate_issue_key
from .jira_client import init_jira_client
from .poller import JiraPoller
from .progress import ProgressTracker
from .repo_resolver import resolve_repo
from .repo_selector import select_repos
from .session import init_session_manager
from .worktree import list_remote_branches

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
        if args.all_statuses:
            status_filter = "AND statusCategory != Done"
        else:
            statuses = args.status or settings.config.batch.statuses
            if len(statuses) == 1:
                status_filter = f'AND status = "{statuses[0]}"'
            else:
                status_list = ", ".join(f'"{s}"' for s in statuses)
                status_filter = f"AND status IN ({status_list})"

        jql = (
            f'assignee = currentUser() AND project = "{args.project}" '
            f"AND sprint in openSprints() "
            f"{status_filter} ORDER BY priority DESC"
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

    # Upfront branch selection — prompt operator for each ticket before batch starts
    if not args.skip_select:
        for event in events:
            if event.issue_key in settings.config.active_branches:
                continue  # already have a branch selection

            candidates = resolve_repo(settings, event.project, event.component, event.issue_key)
            if not candidates:
                continue
            selected = await select_repos(candidates, event, settings)
            if not selected:
                continue

            for repo_mapping in selected:
                branches = await list_remote_branches(repo_mapping.repo)
                if not branches:
                    continue

                # Show ticket info and available branches
                print(f"\n{event.issue_key}: {event.summary}")
                print(f"  Repo: {repo_mapping.repo}")
                ticket_branches = [b for b in branches if event.issue_key.lower() in b.lower()]
                other_branches = [b for b in branches if b not in ticket_branches][:5]
                print(f"  {len(branches)} remote branches available:")
                for b in ticket_branches:
                    print(f"    * {b}")
                for b in other_branches:
                    print(f"      {b}")
                if len(branches) > len(ticket_branches) + 5:
                    print(f"      ... and {len(branches) - len(ticket_branches) - 5} more")

                answer = input("  Branch (enter for fresh, or type/paste name): ").strip()
                if answer:
                    save_active_branch(settings, event.issue_key, answer)
                # If empty, no active_branch saved → _work_repo creates fresh

    is_tty = sys.stderr.isatty()
    tracker = ProgressTracker(events, use_rich=is_tty)

    async with tracker:
        results = await run_batch(events, settings, tracker=tracker)

    succeeded = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    print(f"\nDone: {succeeded} succeeded, {failed} failed")
    for r in results:
        if not r.success:
            print(f"  FAILED {r.issue_key}: {r.error}")


def _default_log_path() -> Path:
    log_dir = Path.home() / ".local" / "share" / "gofer"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "gofer.log"


def _setup_logging(args: argparse.Namespace) -> None:
    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_format = "%(asctime)s %(process)d %(levelname)-8s %(name)s: %(message)s"
    log_path = Path(args.log_file) if args.log_file else _default_log_path()
    log_handlers: list[logging.Handler] = [logging.FileHandler(log_path)]
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=log_handlers,
    )
    logger.info("Logging to %s", log_path)


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
        help="Path to log file (default: ~/.local/share/gofer/gofer.log)",
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

    # Select subcommand — choose a branch for a pending ticket
    select_parser = subparsers.add_parser("select", help="Select a branch for a pending ticket")
    select_parser.add_argument("issue_key", help="Jira issue key (e.g. PROJ-123)")
    select_parser.add_argument("branch", nargs="?", default=None, help="Branch name (omit or --fresh for new branch)")
    select_parser.add_argument("--fresh", action="store_true", help="Start with a fresh branch")
    select_parser.add_argument("--list", dest="list_branches", action="store_true", help="List available branches for the ticket")

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
        "--status",
        nargs="+",
        default=None,
        help='Filter by Jira status categories (default: from config, "To Do")',
    )
    do_parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="Include all non-Done status categories",
    )
    do_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching tickets without working them",
    )
    do_parser.add_argument(
        "--skip-select",
        action="store_true",
        help="Skip interactive branch selection (use saved branches or start fresh)",
    )

    args = parser.parse_args()
    _setup_logging(args)

    try:
        if args.command in ("approve", "reject", "select"):
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

        elif args.command == "select":
            settings = load_settings(args.config)
            if args.list_branches:
                branches = get_pending_branches(args.issue_key, settings)
                if branches is None:
                    print(f"No pending branch selection for {args.issue_key}", file=sys.stderr)
                    sys.exit(1)
                for b in branches:
                    print(b)
            elif args.fresh:
                if set_branch_selection(args.issue_key, _FRESH_SENTINEL, settings):
                    print(f"Starting fresh for {args.issue_key}")
                else:
                    print(f"No pending branch selection for {args.issue_key}", file=sys.stderr)
                    sys.exit(1)
            elif args.branch:
                if set_branch_selection(args.issue_key, args.branch, settings):
                    print(f"Selected branch {args.branch!r} for {args.issue_key}")
                else:
                    print(f"No pending branch selection for {args.issue_key}", file=sys.stderr)
                    sys.exit(1)
            else:
                print("Error: provide a branch name or --fresh or --list", file=sys.stderr)
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
