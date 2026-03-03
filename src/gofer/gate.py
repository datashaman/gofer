from __future__ import annotations

import json
import logging
import re

from claude_code_sdk import ClaudeCodeOptions, TextBlock, query

from .config import GateConfig, Settings
from .models import GateResult, JiraEvent

logger = logging.getLogger(__name__)

_LEVEL_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _level_exceeds(value: str, threshold: str) -> bool:
    """Return True if *value* is strictly above *threshold*."""
    return _LEVEL_ORDER.get(value, 2) > _LEVEL_ORDER.get(threshold, 1)


# ---------------------------------------------------------------------------
# Stage 1 — deterministic heuristics (no I/O)
# ---------------------------------------------------------------------------

def _check_heuristics(event: JiraEvent, gate_config: GateConfig) -> tuple[bool, list[str]]:
    """Check cheap heuristics. Returns (flagged, reasons)."""
    reasons: list[str] = []
    text = f"{event.summary or ''} {event.description or ''}".lower()

    # Risky labels
    for label in event.labels:
        if label.lower() in (l.lower() for l in gate_config.risky_labels):
            reasons.append(f"risky label: {label}")

    # Risky component
    if event.component and event.component.lower() in (
        c.lower() for c in gate_config.risky_components
    ):
        reasons.append(f"risky component: {event.component}")

    # Risky paths mentioned in description
    for path in gate_config.risky_paths:
        if path.lower() in text:
            reasons.append(f"risky path mentioned: {path}")

    # External dependency keywords
    for kw in gate_config.external_dependency_keywords:
        if kw.lower() in text:
            reasons.append(f"external dependency keyword: {kw}")

    flagged = len(reasons) > 0
    return flagged, reasons


# ---------------------------------------------------------------------------
# Stage 2 — Claude judgment call
# ---------------------------------------------------------------------------

_GATE_SYSTEM_PROMPT = (
    "You are a ticket complexity classifier. Assess the Jira ticket and respond "
    "with a JSON object containing exactly these fields:\n"
    '  "complexity": "low" | "medium" | "high"\n'
    '  "risk": "low" | "medium" | "high"\n'
    '  "reasons": [short strings explaining your assessment]\n'
    "Respond ONLY with the JSON object, no other text."
)

_GATE_PROMPT_TEMPLATE = """\
Assess this Jira ticket:

Key: {issue_key}
Summary: {summary}
Description:
{description}

Labels: {labels}
Component: {component}
Status: {status}
"""


def _parse_gate_response(text: str, gate_config: GateConfig) -> GateResult:
    """Extract JSON from Claude's response, validate, and apply approval threshold."""
    # Strip code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse gate JSON, falling back to high/high: %s", text[:200])
        return GateResult(
            complexity="high",
            risk="high",
            needs_approval=True,
            reasons=["gate response could not be parsed — defaulting to safe"],
        )

    complexity = data.get("complexity", "high")
    risk = data.get("risk", "high")
    reasons = data.get("reasons", [])

    # Clamp to valid values
    if complexity not in _LEVEL_ORDER:
        complexity = "high"
    if risk not in _LEVEL_ORDER:
        risk = "high"

    needs_approval = (
        _level_exceeds(complexity, gate_config.require_approval_above)
        or _level_exceeds(risk, gate_config.require_approval_above)
    )

    return GateResult(
        complexity=complexity,
        risk=risk,
        needs_approval=needs_approval,
        reasons=reasons if isinstance(reasons, list) else [str(reasons)],
    )


async def _check_claude_judgment(
    event: JiraEvent,
    worktree_path: str,
    api_key: str,
    gate_config: GateConfig,
) -> GateResult:
    """Stage 2: quick Claude call to classify complexity/risk."""
    prompt = _GATE_PROMPT_TEMPLATE.format(
        issue_key=event.issue_key,
        summary=event.summary,
        description=event.description or "(no description)",
        labels=", ".join(event.labels) if event.labels else "(none)",
        component=event.component or "(none)",
        status=event.status,
    )

    options = ClaudeCodeOptions(
        model="claude-haiku-4-5",
        max_turns=1,
        cwd=worktree_path,
        system_prompt=_GATE_SYSTEM_PROMPT,
        permission_mode="plan",
        env={"ANTHROPIC_API_KEY": api_key},
    )

    response_text = ""
    try:
        async for message in query(prompt=prompt, options=options):
            for block in getattr(message, "content", []):
                if isinstance(block, TextBlock):
                    response_text += block.text
    except Exception:
        logger.exception("Claude gate judgment call failed — defaulting to high/high")
        return GateResult(
            complexity="high",
            risk="high",
            needs_approval=True,
            reasons=["gate Claude call failed — defaulting to safe"],
        )

    if not response_text.strip():
        logger.warning("Empty response from gate Claude call — defaulting to high/high")
        return GateResult(
            complexity="high",
            risk="high",
            needs_approval=True,
            reasons=["gate Claude call returned empty — defaulting to safe"],
        )

    return _parse_gate_response(response_text, gate_config)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def check_gate(
    event: JiraEvent,
    worktree_path: str,
    settings: Settings,
) -> GateResult:
    """Run the two-stage complexity gate. Heuristic flags override Claude judgment."""
    gate_config = settings.config.gates

    # Stage 1: heuristics
    heuristic_flagged, heuristic_reasons = _check_heuristics(event, gate_config)
    if heuristic_flagged:
        logger.info(
            "Gate Stage 1 flagged %s: %s", event.issue_key, heuristic_reasons,
        )

    # Stage 2: Claude judgment (skip if heuristics already require approval)
    if heuristic_flagged:
        gate_result = GateResult(
            complexity="high",
            risk="high",
            needs_approval=True,
            reasons=heuristic_reasons,
        )
    else:
        gate_result = await _check_claude_judgment(
            event, worktree_path, settings.env.anthropic_api_key, gate_config,
        )

    logger.info(
        "Gate result for %s: complexity=%s, risk=%s, needs_approval=%s, reasons=%s",
        event.issue_key,
        gate_result.complexity,
        gate_result.risk,
        gate_result.needs_approval,
        gate_result.reasons,
    )

    return gate_result
