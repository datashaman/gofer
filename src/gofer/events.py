from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from .models import JiraEvent

logger = logging.getLogger(__name__)

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


class InvalidIssueKey(ValueError):
    """Raised when an issue key doesn't match the expected Jira pattern."""


def validate_issue_key(key: str) -> str:
    """Validate and return the issue key, or raise InvalidIssueKey."""
    if not _ISSUE_KEY_RE.match(key):
        raise InvalidIssueKey(f"Invalid issue key: {sanitize_log(key)!r}")
    return key


def sanitize_log(value: str, max_len: int = 200) -> str:
    """Sanitize a string for safe inclusion in log messages.

    Strips control characters and truncates to max_len.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", value)
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "..."
    return cleaned


def _get_issue_key(issue: dict[str, Any]) -> str:
    return validate_issue_key(issue["key"])


def _get_updated(issue: dict[str, Any]) -> datetime:
    raw = issue["fields"]["updated"]
    # Jira returns ISO 8601 with timezone
    return datetime.fromisoformat(raw)


def _build_base_event(issue: dict[str, Any], event_type: str) -> dict[str, Any]:
    fields = issue.get("fields", {})
    components = fields.get("components", [])
    component = components[0]["name"] if components else None
    assignee_field = fields.get("assignee")
    assignee = assignee_field.get("emailAddress") if assignee_field else None
    labels = fields.get("labels", [])
    status_field = fields.get("status", {})
    status_name = status_field.get("name", "Unknown") if status_field else "Unknown"
    project_field = fields.get("project", {})
    project_key = project_field.get("key", "UNKNOWN")

    return {
        "issue_key": _get_issue_key(issue),
        "event_type": event_type,
        "project": project_key,
        "component": component,
        "summary": fields.get("summary", ""),
        "description": fields.get("description"),
        "status": status_name,
        "assignee": assignee,
        "labels": labels,
        "updated": _get_updated(issue),
        "raw": issue,
    }


def classify_changes(
    issue: dict[str, Any],
    previous_state: dict[str, Any] | None,
    my_email: str,
) -> list[JiraEvent]:
    """Compare current issue against previous state and return detected events."""
    events: list[JiraEvent] = []
    fields = issue.get("fields", {})
    prev_fields = previous_state.get("fields", {}) if previous_state else {}

    # First time seeing this issue — check if assigned to me
    if previous_state is None:
        assignee = fields.get("assignee")
        if assignee and assignee.get("emailAddress") == my_email:
            events.append(JiraEvent(
                **_build_base_event(issue, "assigned_to_me"),
                fields_changed=["assignee"],
            ))
        return events

    changed_fields: list[str] = []

    # Check assignee change
    old_assignee = prev_fields.get("assignee")
    new_assignee = fields.get("assignee")
    old_email = old_assignee.get("emailAddress") if old_assignee else None
    new_email = new_assignee.get("emailAddress") if new_assignee else None
    if old_email != new_email:
        changed_fields.append("assignee")
        if new_email == my_email:
            events.append(JiraEvent(
                **_build_base_event(issue, "assigned_to_me"),
                fields_changed=["assignee"],
            ))

    # Check status change
    old_status = prev_fields.get("status", {})
    new_status = fields.get("status", {})
    old_status_name = old_status.get("name") if old_status else None
    new_status_name = new_status.get("name") if new_status else None
    if old_status_name != new_status_name:
        changed_fields.append("status")
        events.append(JiraEvent(
            **_build_base_event(issue, "status_changed"),
            fields_changed=["status"],
        ))

    # Check labels change
    old_labels = set(prev_fields.get("labels", []))
    new_labels = set(fields.get("labels", []))
    if old_labels != new_labels:
        changed_fields.append("labels")
        events.append(JiraEvent(
            **_build_base_event(issue, "labeled"),
            fields_changed=["labels"],
        ))

    # Check for new comments
    old_comments = prev_fields.get("comment", {}).get("comments", [])
    new_comments = fields.get("comment", {}).get("comments", [])
    if len(new_comments) > len(old_comments):
        changed_fields.append("comment")
        # Check latest comments for mentions
        for comment in new_comments[len(old_comments):]:
            body = comment.get("body", "")
            if my_email in body or f"[~{my_email}]" in body:
                events.append(JiraEvent(
                    **_build_base_event(issue, "mentioned"),
                    fields_changed=["comment"],
                ))
                break
        # Always emit a commented event for new comments
        events.append(JiraEvent(
            **_build_base_event(issue, "commented"),
            fields_changed=["comment"],
        ))

    # Check description/summary changes (mention check)
    for field_name in ("summary", "description"):
        old_val = prev_fields.get(field_name, "")
        new_val = fields.get(field_name, "")
        if old_val != new_val:
            changed_fields.append(field_name)
            if new_val and my_email in str(new_val):
                events.append(JiraEvent(
                    **_build_base_event(issue, "mentioned"),
                    fields_changed=[field_name],
                ))

    # Generic "updated" fallback if something changed but no specific event
    if changed_fields and not events:
        events.append(JiraEvent(
            **_build_base_event(issue, "updated"),
            fields_changed=changed_fields,
        ))

    return events
