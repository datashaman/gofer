from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "assigned_to_me",
    "status_changed",
    "mentioned",
    "commented",
    "labeled",
    "updated",
]


class JiraEvent(BaseModel):
    issue_key: str
    event_type: EventType
    project: str
    component: str | None = None
    summary: str
    description: str | None = None
    status: str
    assignee: str | None = None
    labels: list[str] = Field(default_factory=list)
    fields_changed: list[str] = Field(default_factory=list)
    updated: datetime
    raw: dict[str, Any] = Field(default_factory=dict)


class GateResult(BaseModel):
    complexity: Literal["low", "medium", "high"]
    risk: Literal["low", "medium", "high"]
    needs_approval: bool
    reasons: list[str] = Field(default_factory=list)


