from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    jira_url: str
    jira_email: str
    jira_api_token: str
    anthropic_api_key: str


class RepoMapping(BaseModel):
    repo: str
    branch: str = "main"


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


class ConcurrencyConfig(BaseModel):
    max_parallel_sessions: int = 3
    session_timeout: int = 3600


class YamlConfig(BaseModel):
    poll_interval: int = 60
    projects: dict[str, RepoMapping] = Field(default_factory=dict)
    gates: GateConfig = Field(default_factory=GateConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)


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
        yaml_config = YamlConfig()

    env = EnvSettings()  # type: ignore[call-arg]
    return Settings(env=env, config=yaml_config)
