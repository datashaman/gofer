from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from ..dispatcher import handles
from ..jira_client import add_comment
from ..models import JiraEvent
from ..session import SessionResult, get_session_manager
from ..worktree import create_worktree

logger = logging.getLogger(__name__)


def _extract_latest_comment(event: JiraEvent) -> tuple[str, str] | None:
    """Return (author_email, body) from the most recent comment, or None."""
    comments: list[dict[str, Any]] = (
        event.raw.get("fields", {}).get("comment", {}).get("comments", [])
    )
    if not comments:
        return None
    latest = comments[-1]
    author = latest.get("author", {}).get("emailAddress", "unknown")
    body = latest.get("body", "")
    return author, body


def _build_mention_system_prompt(event: JiraEvent) -> str:
    parts = [
        "You are an AI assistant that has been mentioned in a Jira ticket comment.",
        "Your job is to read the codebase and respond helpfully to the question or request.",
        "Do NOT make code changes or create commits — only provide a text response.",
        f"\nTicket: {event.issue_key}",
        f"Summary: {event.summary}",
        f"Status: {event.status}",
    ]
    if event.description:
        parts.append(f"Description:\n{event.description}")
    return "\n\n".join(parts)


def _build_mention_prompt(event: JiraEvent, comment_body: str, author: str) -> str:
    return (
        f"{author} mentioned you in a comment on {event.issue_key}:\n\n"
        f"---\n{comment_body}\n---\n\n"
        "Read the codebase as needed to answer their question or address their request. "
        "Provide a clear, concise response. Do NOT make code changes or create commits."
    )


@handles("mentioned")
async def handle_mention(event: JiraEvent, settings: Settings) -> None:
    """Handle @-mentions — spawn a Claude session and post the response back to Jira."""
    session_manager = get_session_manager()
    if session_manager is None:
        logger.error("Session manager not initialized — skipping mention on %s", event.issue_key)
        return

    # Extract latest comment
    comment = _extract_latest_comment(event)
    if comment is None:
        logger.warning("No comment found for mention event on %s — skipping", event.issue_key)
        return
    author, comment_body = comment

    # Self-reply guard
    if author == settings.env.jira_email:
        logger.debug("Ignoring self-mention on %s", event.issue_key)
        return

    # Resolve repo mapping
    repo_mapping = settings.config.projects.get(event.project)
    if repo_mapping is None:
        logger.warning(
            "No repo mapping for project %s — cannot handle mention on %s",
            event.project,
            event.issue_key,
        )
        return

    logger.info(
        "[mention] %s on %s by %s — spawning session",
        event.event_type,
        event.issue_key,
        author,
    )

    # Create/reuse worktree
    try:
        worktree = await create_worktree(
            repo_path=repo_mapping.repo,
            issue_key=event.issue_key,
            base_branch=repo_mapping.branch,
        )
    except Exception:
        logger.exception("Failed to create worktree for mention on %s", event.issue_key)
        return

    # Run Claude session
    result: SessionResult = await session_manager.run_session(
        issue_key=event.issue_key,
        prompt=_build_mention_prompt(event, comment_body, author),
        cwd=worktree.worktree_path,
        system_prompt=_build_mention_system_prompt(event),
        model="claude-sonnet-4-6",
        max_turns=15,
        env={"ANTHROPIC_API_KEY": settings.env.anthropic_api_key},
    )

    if result.success and result.response_text:
        try:
            await add_comment(event.issue_key, result.response_text)
        except Exception:
            logger.exception("Failed to post mention response to %s", event.issue_key)
    elif not result.success:
        logger.error("Mention session for %s failed: %s", event.issue_key, result.error)
    else:
        logger.warning("Mention session for %s produced no response text", event.issue_key)
