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

NO_RESPONSE_NEEDED = "NO_RESPONSE_NEEDED"


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


def _is_mention(body: str, my_email: str) -> bool:
    """Check if the comment body contains a mention of the agent's email."""
    return my_email in body or f"[~{my_email}]" in body


def _build_comment_system_prompt(event: JiraEvent) -> str:
    parts = [
        "You are an AI assistant monitoring comments on a Jira ticket.",
        "A new comment was posted. Decide if a response is warranted.",
        f"If the comment is purely informational and needs no reply, respond with exactly: {NO_RESPONSE_NEEDED}",
        "Otherwise, provide a helpful response. Do NOT make code changes or create commits.",
        f"\nTicket: {event.issue_key}",
        f"Summary: {event.summary}",
        f"Status: {event.status}",
    ]
    if event.description:
        parts.append(f"Description:\n{event.description}")
    return "\n\n".join(parts)


def _build_comment_prompt(event: JiraEvent, comment_body: str, author: str) -> str:
    return (
        f"{author} commented on {event.issue_key}:\n\n"
        f"---\n{comment_body}\n---\n\n"
        "Read the codebase as needed. If this comment asks a question or requests action, "
        "provide a clear, concise response. "
        f"If the comment is purely informational and needs no reply, respond with exactly: {NO_RESPONSE_NEEDED}\n"
        "Do NOT make code changes or create commits."
    )


@handles("commented")
async def handle_comment(event: JiraEvent, settings: Settings) -> None:
    """Handle new comments — spawn a Claude session and post the response back to Jira."""
    session_manager = get_session_manager()
    if session_manager is None:
        logger.error("Session manager not initialized — skipping comment on %s", event.issue_key)
        return

    # Extract latest comment
    comment = _extract_latest_comment(event)
    if comment is None:
        logger.warning("No comment found for comment event on %s — skipping", event.issue_key)
        return
    author, comment_body = comment

    # Self-reply guard
    if author == settings.env.jira_email:
        logger.debug("Ignoring own comment on %s", event.issue_key)
        return

    # Skip mentions — the mention handler covers those
    if _is_mention(comment_body, settings.env.jira_email):
        logger.debug("Comment on %s is a mention — deferring to mention handler", event.issue_key)
        return

    # Resolve repo mapping
    repo_mapping = settings.config.projects.get(event.project)
    if repo_mapping is None:
        logger.warning(
            "No repo mapping for project %s — cannot handle comment on %s",
            event.project,
            event.issue_key,
        )
        return

    logger.info(
        "[comment] %s on %s by %s — spawning session",
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
        logger.exception("Failed to create worktree for comment on %s", event.issue_key)
        return

    # Run Claude session
    result: SessionResult = await session_manager.run_session(
        issue_key=event.issue_key,
        prompt=_build_comment_prompt(event, comment_body, author),
        cwd=worktree.worktree_path,
        system_prompt=_build_comment_system_prompt(event),
        model="claude-sonnet-4-6",
        max_turns=15,
        env={"ANTHROPIC_API_KEY": settings.env.anthropic_api_key},
    )

    if not result.success:
        logger.error("Comment session for %s failed: %s", event.issue_key, result.error)
        return

    if not result.response_text:
        logger.warning("Comment session for %s produced no response text", event.issue_key)
        return

    # Check for sentinel — Claude decided no response is needed
    if NO_RESPONSE_NEEDED in result.response_text.strip():
        logger.info("Claude determined no response needed for comment on %s", event.issue_key)
        return

    try:
        await add_comment(event.issue_key, result.response_text)
    except Exception:
        logger.exception("Failed to post comment response to %s", event.issue_key)
