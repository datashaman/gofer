from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .models import GateResult

logger = logging.getLogger(__name__)


def _pending_path(settings: Settings) -> Path:
    return Path(settings.config.approvals.pending_file)


def _read_pending(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read pending approvals from %s", path)
        return []


def _write_pending(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write entries atomically via tmpfile + os.replace."""
    fd, tmp = tempfile.mkstemp(dir=path.parent or Path("."), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def prompt_approval(
    issue_key: str,
    gate_result: GateResult,
    settings: Settings,
) -> bool:
    """Write a pending entry and poll until a decision is made or timeout expires.

    Returns True if approved, False if rejected or timed out.
    """
    path = _pending_path(settings)
    timeout = settings.config.approvals.timeout

    # Write pending entry
    entries = _read_pending(path)
    entry = {
        "issue_key": issue_key,
        "complexity": gate_result.complexity,
        "risk": gate_result.risk,
        "reasons": gate_result.reasons,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "decision": None,
    }
    entries.append(entry)
    _write_pending(path, entries)

    logger.info(
        "Approval needed for %s — run 'jira-agent approve %s' to approve",
        issue_key,
        issue_key,
    )

    # Poll for decision
    elapsed = 0
    poll_interval = 5
    decision = None

    while elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        current = _read_pending(path)
        for e in current:
            if e["issue_key"] == issue_key and e["decision"] is not None:
                decision = e["decision"]
                break
        if decision is not None:
            break

    # Clean up entry from file
    current = _read_pending(path)
    remaining = [e for e in current if e["issue_key"] != issue_key]
    _write_pending(path, remaining)

    if decision == "approved":
        logger.info("Operator approved %s", issue_key)
        return True

    if decision == "rejected":
        logger.info("Operator rejected %s", issue_key)
    else:
        logger.info("Approval timed out for %s after %ds", issue_key, timeout)

    return False


def set_decision(issue_key: str, decision: str, settings: Settings) -> bool:
    """Set the decision for a pending approval. Returns False if issue_key not found."""
    path = _pending_path(settings)
    entries = _read_pending(path)

    found = False
    for entry in entries:
        if entry["issue_key"] == issue_key and entry["decision"] is None:
            entry["decision"] = decision
            found = True
            break

    if not found:
        return False

    _write_pending(path, entries)
    return True
