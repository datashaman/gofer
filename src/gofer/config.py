from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    jira_url: str
    jira_email: str
    jira_api_token: str
    anthropic_api_key: str


class RepoMapping(BaseModel):
    repo: str
    branch: str = "main"

    @model_validator(mode="before")
    @classmethod
    def _expand_repo_path(cls, data: Any) -> Any:
        if isinstance(data, dict) and "repo" in data:
            data["repo"] = str(Path(data["repo"]).expanduser())
        return data


class ProjectConfig(BaseModel):
    default: list[RepoMapping] = Field(default_factory=list)
    components: dict[str, list[RepoMapping]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_repo_mappings(cls, data: Any) -> Any:
        """Normalize single RepoMapping dicts into one-element lists.

        Accepts both ``{ repo: ..., branch: ... }`` and
        ``[{ repo: ..., branch: ... }, ...]`` for ``default`` and each
        component entry.
        """
        if not isinstance(data, dict):
            return data
        # Normalize default
        default = data.get("default")
        if isinstance(default, dict) and "repo" in default:
            data["default"] = [default]
        # Normalize components
        components = data.get("components")
        if isinstance(components, dict):
            for key, value in components.items():
                if isinstance(value, dict) and "repo" in value:
                    components[key] = [value]
        return data


class SlackConfig(BaseModel):
    webhook_url: str
    channel: str = "#gofer"


class ApprovalsConfig(BaseModel):
    pending_file: str = "pending_approvals.json"
    timeout: int = 3600


class GateConfig(BaseModel):
    max_complexity: Literal["low", "medium", "high"] = "medium"
    max_risk: Literal["low", "medium", "high"] = "medium"
    require_approval_above: Literal["low", "medium", "high"] = "medium"
    # Stage 1 heuristic thresholds
    risky_labels: list[str] = [
        "epic", "security", "migration", "breaking-change", "infra", "refactor",
    ]
    risky_components: list[str] = [
        "auth", "billing", "payments", "permissions", "data",
    ]
    risky_paths: list[str] = [
        "db/migrations/", "terraform/", "k8s/", "infra/", "policies/",
    ]
    max_auto_files: int = 10
    max_auto_loc: int = 200
    external_dependency_keywords: list[str] = [
        "depends on", "waiting on", "blocked by", "external",
    ]


class BatchConfig(BaseModel):
    statuses: list[str] = Field(default_factory=lambda: ["To Do"])


class ConcurrencyConfig(BaseModel):
    max_parallel_sessions: int = 3
    session_timeout: int = 3600


class YamlConfig(BaseModel):
    poll_interval: int = 60
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    gates: GateConfig = Field(default_factory=GateConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    slack: SlackConfig | None = None
    approvals: ApprovalsConfig = Field(default_factory=ApprovalsConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_projects(cls, data: Any) -> Any:
        """Auto-migrate old flat project format to new ProjectConfig format.

        Old: ``PROJ: { repo: /path, branch: main }``
        New: ``PROJ: { default: { repo: /path, branch: main }, components: {} }``
        """
        if not isinstance(data, dict):
            return data
        projects = data.get("projects")
        if not isinstance(projects, dict):
            return data
        migrated = {}
        for key, value in projects.items():
            if isinstance(value, dict) and "repo" in value and "default" not in value:
                migrated[key] = {"default": [value], "components": {}}
            else:
                migrated[key] = value
        data["projects"] = migrated
        return data


class Settings(BaseModel):
    env: EnvSettings
    config: YamlConfig


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    config_path = Path(config_path)
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        yaml_config = YamlConfig(**raw)
    else:
        logger.warning("Config file %s not found — using defaults", config_path)
        yaml_config = YamlConfig()

    env = EnvSettings()  # type: ignore[call-arg]
    return Settings(env=env, config=yaml_config)
