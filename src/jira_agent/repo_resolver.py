from __future__ import annotations

import logging

from .config import RepoMapping, Settings

logger = logging.getLogger(__name__)


def resolve_repo(
    settings: Settings,
    project: str,
    component: str | None,
    issue_key: str,
) -> RepoMapping | None:
    """Resolve a repo mapping for the given project and optional component.

    Resolution order:
    1. ``projects[project].components[component]`` — exact component match
    2. ``projects[project].default`` — project fallback
    3. ``None`` — project not configured
    """
    project_config = settings.config.projects.get(project)
    if project_config is None:
        logger.warning(
            "No repo mapping for project %s — cannot handle %s",
            project,
            issue_key,
        )
        return None

    if component and component in project_config.components:
        logger.debug(
            "Resolved %s via component %s in project %s",
            issue_key,
            component,
            project,
        )
        return project_config.components[component]

    return project_config.default
