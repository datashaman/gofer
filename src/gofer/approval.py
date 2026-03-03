from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .models import GateResult

logger = logging.getLogger(__name__)

_VALID_DECISIONS = {"approved", "rejected"}


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Acquire an exclusive lock on a .lock file adjacent to *path*."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    """Write entries atomically via tmpfile + os.replace, with 0o600 perms."""
    fd, tmp = tempfile.mkstemp(dir=path.parent or Path("."), suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(entries, f, indent=2, default=str)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
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

    # Write pending entry (locked)
    with _file_lock(path):
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
        "Approval needed for %s — run 'gofer approve %s' to approve",
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

    # Clean up entry from file (locked)
    with _file_lock(path):
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


_FRESH_SENTINEL = "__fresh__"


async def prompt_branch_select(
    issue_key: str,
    branches: list[str],
    settings: Settings,
) -> str | None:
    """Write a pending branch_select entry and poll until the operator chooses.

    Returns the selected branch name, or ``None`` for fresh start (including
    timeout and the ``__fresh__`` sentinel).
    """
    path = _pending_path(settings)
    timeout = settings.config.approvals.timeout

    with _file_lock(path):
        entries = _read_pending(path)
        entry = {
            "issue_key": issue_key,
            "type": "branch_select",
            "branches": branches,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "decision": None,
        }
        entries.append(entry)
        _write_pending(path, entries)

    logger.info(
        "Branch selection needed for %s (%d branches) — run 'gofer select %s <branch>' or 'gofer select %s --fresh'",
        issue_key,
        len(branches),
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
            if (
                e["issue_key"] == issue_key
                and e.get("type") == "branch_select"
                and e["decision"] is not None
            ):
                decision = e["decision"]
                break
        if decision is not None:
            break

    # Clean up entry
    with _file_lock(path):
        current = _read_pending(path)
        remaining = [
            e for e in current
            if not (e["issue_key"] == issue_key and e.get("type") == "branch_select")
        ]
        _write_pending(path, remaining)

    if decision and decision != _FRESH_SENTINEL:
        logger.info("Operator selected branch %r for %s", decision, issue_key)
        return decision

    if decision == _FRESH_SENTINEL:
        logger.info("Operator chose fresh start for %s", issue_key)
    else:
        logger.info(
            "Branch selection timed out for %s after %ds — starting fresh",
            issue_key, timeout,
        )

    return None


def set_branch_selection(issue_key: str, branch: str, settings: Settings) -> bool:
    """Set branch selection for a pending branch_select entry."""
    path = _pending_path(settings)
    with _file_lock(path):
        entries = _read_pending(path)
        for entry in entries:
            if (
                entry["issue_key"] == issue_key
                and entry.get("type") == "branch_select"
                and entry["decision"] is None
            ):
                entry["decision"] = branch
                _write_pending(path, entries)
                return True
    return False


def get_pending_branches(issue_key: str, settings: Settings) -> list[str] | None:
    """Return the branch list for a pending branch_select, or None if not found."""
    path = _pending_path(settings)
    entries = _read_pending(path)
    for entry in entries:
        if (
            entry["issue_key"] == issue_key
            and entry.get("type") == "branch_select"
            and entry["decision"] is None
        ):
            return entry.get("branches", [])
    return None


def set_decision(issue_key: str, decision: str, settings: Settings) -> bool:
    """Set the decision for a pending approval. Returns False if issue_key not found."""
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"Invalid decision {decision!r}, must be one of {_VALID_DECISIONS}")

    path = _pending_path(settings)

    with _file_lock(path):
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
