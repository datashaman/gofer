from __future__ import annotations

import logging

from ..approval import prompt_approval
from ..config import Settings
from ..dispatcher import handles
from ..gate import check_gate
from ..models import JiraEvent
from ..session import SessionManager, SessionResult
from ..worktree import create_worktree

logger = logging.getLogger(__name__)

_session_manager: SessionManager | None = None
_completed: set[str] = set()


def init_session_manager(settings: Settings) -> SessionManager:
    """Initialize the module-level session manager. Called once from main.py."""
    global _session_manager
    concurrency = settings.config.concurrency
    _session_manager = SessionManager(
        max_parallel=concurrency.max_parallel_sessions,
        session_timeout=concurrency.session_timeout,
    )
    return _session_manager


def _build_system_prompt(event: JiraEvent) -> str:
    parts = [
        "You are an autonomous software engineer working on a Jira ticket.",
        f"Ticket: {event.issue_key}",
        f"Summary: {event.summary}",
        f"Status: {event.status}",
    ]
    if event.description:
        parts.append(f"Description:\n{event.description}")
    if event.labels:
        parts.append(f"Labels: {', '.join(event.labels)}")
    if event.component:
        parts.append(f"Component: {event.component}")
    return "\n\n".join(parts)


def _build_prompt(event: JiraEvent) -> str:
    return (
        f"Implement the work described in Jira ticket {event.issue_key}.\n\n"
        "Steps:\n"
        "1. Read the codebase to understand the project structure and conventions.\n"
        "2. Implement the changes described in the ticket.\n"
        "3. Commit your changes with a clear commit message referencing the ticket key.\n"
        "4. Push your branch to origin.\n"
        "5. Create a pull request with a summary of what you did.\n\n"
        "Work autonomously. If anything is unclear from the ticket description, "
        "make reasonable assumptions and document them in the PR description."
    )


@handles("assigned_to_me", "status_changed")
async def handle_ticket_work(event: JiraEvent, settings: Settings) -> None:
    """Handle ticket assignment and status changes — resolve repo, create worktree, spawn Claude session."""
    if _session_manager is None:
        logger.error("Session manager not initialized — skipping %s", event.issue_key)
        return

    if _session_manager.is_active(event.issue_key):
        logger.info("Session already active for %s — skipping", event.issue_key)
        return

    if event.issue_key in _completed:
        logger.info("Session already completed for %s — skipping", event.issue_key)
        return

    # Resolve repo mapping from project key
    repo_mapping = settings.config.projects.get(event.project)
    if repo_mapping is None:
        logger.warning(
            "No repo mapping for project %s — cannot handle %s",
            event.project,
            event.issue_key,
        )
        return

    logger.info(
        "[ticket_work] %s: %s — %s (%s) → repo=%s branch=%s",
        event.event_type,
        event.issue_key,
        event.summary,
        event.status,
        repo_mapping.repo,
        repo_mapping.branch,
    )

    # Create worktree
    try:
        worktree = await create_worktree(
            repo_path=repo_mapping.repo,
            issue_key=event.issue_key,
            base_branch=repo_mapping.branch,
        )
    except Exception:
        logger.exception("Failed to create worktree for %s", event.issue_key)
        return

    # Complexity gate
    gate_result = await check_gate(event, worktree.worktree_path, settings)
    if gate_result.needs_approval:
        approved = await prompt_approval(event.issue_key, gate_result)
        if not approved:
            logger.info("Operator rejected %s — skipping session", event.issue_key)
            return

    # Run Claude Code session
    result: SessionResult = await _session_manager.run_session(
        issue_key=event.issue_key,
        prompt=_build_prompt(event),
        cwd=worktree.worktree_path,
        system_prompt=_build_system_prompt(event),
        model="claude-sonnet-4-6",
        max_turns=30,
        env={"ANTHROPIC_API_KEY": settings.env.anthropic_api_key},
    )

    if result.success:
        _completed.add(event.issue_key)
        logger.info(
            "Session for %s completed successfully: turns=%d, cost=$%.4f",
            event.issue_key,
            result.num_turns,
            result.cost_usd or 0,
        )
    else:
        logger.error(
            "Session for %s failed: %s",
            event.issue_key,
            result.error,
        )
