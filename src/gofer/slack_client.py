from __future__ import annotations

import logging

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


async def post_slack(settings: Settings, text: str) -> None:
    """Post a message to Slack via webhook. No-op if Slack is not configured."""
    slack = settings.config.slack
    if slack is None:
        logger.debug("Slack not configured — skipping notification")
        return

    payload = {"text": text, "channel": slack.channel}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(slack.webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to post Slack notification")


def format_session_result(
    issue_key: str,
    success: bool,
    cost_usd: float | None,
    num_turns: int,
    error: str | None,
) -> str:
    """Format a session result for Slack."""
    status = "completed successfully" if success else "failed"
    parts = [f"*{issue_key}* session {status}"]
    if success:
        parts.append(f"turns={num_turns}, cost=${cost_usd or 0:.4f}")
    if error:
        parts.append(f"error: {error}")
    return " — ".join(parts)


def format_approval_needed(
    issue_key: str,
    complexity: str,
    risk: str,
    reasons: list[str],
) -> str:
    """Format an approval-needed notification for Slack."""
    parts = [
        f"*{issue_key}* needs approval (complexity={complexity}, risk={risk})",
        "Reasons: " + "; ".join(reasons) if reasons else "",
        f"Run `gofer approve {issue_key}` to approve or `gofer reject {issue_key}` to reject.",
    ]
    return "\n".join(p for p in parts if p)
