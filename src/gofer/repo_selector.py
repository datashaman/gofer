from __future__ import annotations

import json
import logging
import re

from claude_code_sdk import ClaudeCodeOptions, TextBlock, query

from .config import RepoMapping, Settings
from .models import JiraEvent

logger = logging.getLogger(__name__)

_SELECTOR_SYSTEM_PROMPT = (
    "You are a repo selector. Given a Jira ticket and a list of candidate repositories, "
    "determine which repos need changes to fulfill the ticket.\n\n"
    "Respond ONLY with a JSON array of repo paths (strings) that are relevant. "
    "Include ALL repos that would need changes — it could be one or many.\n"
    "Example: [\"/path/to/repo-a\", \"/path/to/repo-b\"]"
)

_SELECTOR_PROMPT_TEMPLATE = """\
Which of these repos need changes for this Jira ticket?

Ticket: {issue_key}
Summary: {summary}
Description:
{description}

Candidate repos:
{candidates}
"""


def _parse_selector_response(
    text: str, candidates: list[RepoMapping],
) -> list[RepoMapping]:
    """Parse Claude's JSON array response and match back to RepoMapping objects."""
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")

    try:
        paths = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(
            "Failed to parse repo selector JSON, returning all candidates: %s",
            text[:200],
        )
        return candidates

    if not isinstance(paths, list):
        logger.warning("Repo selector returned non-list, returning all candidates")
        return candidates

    # Normalize for matching
    path_set = {str(p).rstrip("/") for p in paths}
    matched = [c for c in candidates if c.repo.rstrip("/") in path_set]

    if not matched:
        logger.warning(
            "Repo selector returned no matching paths (%s), returning all candidates",
            paths,
        )
        return candidates

    return matched


async def select_repos(
    candidates: list[RepoMapping],
    event: JiraEvent,
    settings: Settings,
) -> list[RepoMapping]:
    """Select relevant repos from candidates using a lightweight Claude call.

    If there's only one candidate, returns it directly (no Claude call).
    For multiple candidates, asks Claude which repos are relevant to the ticket.
    """
    if len(candidates) <= 1:
        return candidates

    candidate_lines = "\n".join(
        f"- {c.repo} (branch: {c.branch})" for c in candidates
    )
    prompt = _SELECTOR_PROMPT_TEMPLATE.format(
        issue_key=event.issue_key,
        summary=event.summary,
        description=event.description or "(no description)",
        candidates=candidate_lines,
    )

    options = ClaudeCodeOptions(
        model="claude-haiku-4-5",
        max_turns=1,
        system_prompt=_SELECTOR_SYSTEM_PROMPT,
        permission_mode="plan",
        env={"ANTHROPIC_API_KEY": settings.env.anthropic_api_key, "CLAUDECODE": ""},
    )

    response_text = ""
    try:
        async for message in query(prompt=prompt, options=options):
            for block in getattr(message, "content", []):
                if isinstance(block, TextBlock):
                    response_text += block.text
    except Exception:
        logger.exception(
            "Repo selector Claude call failed for %s — returning all candidates",
            event.issue_key,
        )
        return candidates

    if not response_text.strip():
        logger.warning(
            "Empty response from repo selector for %s — returning all candidates",
            event.issue_key,
        )
        return candidates

    selected = _parse_selector_response(response_text, candidates)
    logger.info(
        "Repo selector for %s: %d/%d repos selected — %s",
        event.issue_key,
        len(selected),
        len(candidates),
        [r.repo for r in selected],
    )
    return selected
