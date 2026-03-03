from __future__ import annotations

import logging

from .config import RepoMapping, Settings

logger = logging.getLogger(__name__)


def resolve_repo(
    settings: Settings,
    project: str,
    component: str | None,
    issue_key: str,
) -> list[RepoMapping] | None:
    """Resolve repo mapping(s) for the given project and optional component.

    Resolution order:
    1. ``projects[project].components[component]`` — exact component match (may be multiple)
    2. ``projects[project].default`` — project fallback (may be multiple)
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

    if not project_config.default:
        logger.warning(
            "No component match and no default for project %s — cannot handle %s",
            project,
            issue_key,
        )
        return None

    return project_config.default
