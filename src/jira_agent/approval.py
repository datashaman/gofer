from __future__ import annotations

import asyncio
import logging
import sys

from .models import GateResult

logger = logging.getLogger(__name__)


def _format_gate_summary(issue_key: str, gate_result: GateResult) -> str:
    """Format a boxed terminal summary of the gate result."""
    lines = [
        f"  Ticket:     {issue_key}",
        f"  Complexity: {gate_result.complexity}",
        f"  Risk:       {gate_result.risk}",
    ]
    if gate_result.reasons:
        lines.append("  Reasons:")
        for reason in gate_result.reasons:
            lines.append(f"    - {reason}")

    width = max(len(line) for line in lines) + 2
    border = "+" + "-" * width + "+"
    padded = [f"|{line:<{width}}|" for line in lines]

    title = " GATE CHECK "
    title_line = "+" + title.center(width, "-") + "+"

    return "\n".join([title_line, *padded, border])


async def prompt_approval(issue_key: str, gate_result: GateResult) -> bool:
    """Show gate summary and ask the operator to approve or reject.

    Uses run_in_executor to read stdin without blocking the event loop.
    Prints to stderr so it's visible even when stdout is redirected.
    Returns True (approved) or False (rejected). EOF defaults to reject.
    """
    summary = _format_gate_summary(issue_key, gate_result)
    print(summary, file=sys.stderr)
    print(
        f"\nApprove work on {issue_key}? [y/N] ",
        end="",
        flush=True,
        file=sys.stderr,
    )

    loop = asyncio.get_running_loop()
    try:
        line = await loop.run_in_executor(None, sys.stdin.readline)
    except EOFError:
        logger.info("EOF on stdin — rejecting %s", issue_key)
        return False

    answer = line.strip().lower() if line else ""
    approved = answer in ("y", "yes")

    if approved:
        logger.info("Operator approved %s", issue_key)
    else:
        logger.info("Operator rejected %s", issue_key)

    return approved
