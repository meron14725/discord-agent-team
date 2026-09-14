import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .roles import RoleId, RoleRegistry, default_role_registry


class Check(BaseModel):
    name: str
    app_id: int


class Repo(BaseModel):
    description: str = ""
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base: str = "master"
    per_task: bool = False
    private: bool = True
    template_repository: str = ""
    publisher_login: str = "agent-publisher[bot]"
    reviewer_login: str = "agent-reviewer[bot]"
    checks: list[Check] = Field(default_factory=list)
    test_commands: list[list[str]] = Field(default_factory=lambda: [["python", "-m", "unittest", "discover"]])
    allowed_paths: list[str] = Field(
        default_factory=lambda: ["src/*", "tests/*", "docs/tasks/*", "docs/work-items/*"]
    )


class ReviewerApp(BaseModel):
    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)


class DiscordSRE(BaseModel):
    enabled: bool = False
    managed_category_ids: list[str] = Field(default_factory=list)
    protected_channel_ids: list[str] = Field(default_factory=list)
    protected_role_ids: list[str] = Field(default_factory=list)
    audit_channel_id: str = ""
    require_approval: bool = True
    approval_seconds: int = Field(default=900, ge=60, le=86400)


class WorkflowV2(BaseModel):
    enabled: bool = False
    repository_aliases: list[str] = Field(default_factory=list)
    requirements_approver_ids: list[str] = Field(default_factory=list)
    plan_approver_ids: list[str] = Field(default_factory=list)
    project_channels: dict[str, str] = Field(default_factory=dict)
    github_issue_conditional_updates: bool = False
    normal_concurrency: int = Field(default=4, ge=1, le=4)
    privileged_concurrency: int = Field(default=1, ge=1, le=1)
    model_calls_per_task: int = Field(default=150, ge=1, le=150)
    consultations_per_task: int = Field(default=75, ge=1, le=75)
    consultations_per_topic: int = Field(default=25, ge=1, le=25)
    plan_revision_limit: int = Field(default=15, ge=1, le=15)
    implementation_revision_limit: int = Field(default=15, ge=1, le=15)
    heartbeat_seconds: int = Field(default=120, ge=30, le=120)
    stalled_seconds: int = Field(default=600, ge=300, le=1800)
    renderer_url: str = "http://renderer:8091"
    worker_url: str = "http://v2-worker:8090"

    @model_validator(mode="after")
    def safe_rollout(self):
        if len(self.repository_aliases) != len(set(self.repository_aliases)):
            raise ValueError("v2 repository aliases must be unique")
        if self.enabled and not self.repository_aliases:
            raise ValueError("v2 requires an explicit repository allowlist")
        return self


class MaintenanceAuthorization(BaseModel):
    repository: str
    requirements_hash: str
    plan_hash: str
    owner_id: str
    paths: list[str]


class Settings(BaseModel):
    maintenance_authorizations: dict[str, MaintenanceAuthorization] = Field(default_factory=dict)
    github_reviewer_app: ReviewerApp | None = None
    mode: Literal["mock", "live"] = "mock"
    merge_mode: Literal["disabled", "human_gate", "auto_low_risk"] = "human_gate"
    guild_id: str = "demo-guild"
    channel_id: str = "demo-channel"
    owner_ids: list[str] = Field(default_factory=lambda: ["demo-owner"])
    repos: dict[str, Repo] = Field(default_factory=lambda: {"demo": Repo(repository="example/demo")})
    default_repo: str = ""
    model: str = ""
    auth_mode: Literal["chatgpt", "api_key"] = "chatgpt"
    daily_run_limit: int = Field(default=150, ge=1)
    task_run_limit: int = Field(default=50, ge=1)
    daily_budget_usd: float = 0
    task_budget_usd: float = 0
    # Reservation is conservatively charged even if a run fails; no invented exact dollar usage.
    run_reservation_usd: float = 0
    max_rounds: int = 3
    lease_seconds: int = 90
    run_timeout: int = 1200
    poll_seconds: int = 60
    approval_seconds: int = 86400
    max_files: int = 100
    max_bytes: int = 2_000_000
    auto_max_lines: int = 200
    message_content: bool = False
    natural_language_requests: bool = False
    coordination_timeout: int = Field(default=300, ge=30, le=600)
    specialist_concurrency: int = Field(default=1, ge=1, le=4)
    specialist_retry_attempts: int = Field(default=3, ge=1, le=3)
    specialist_continuation_limit: int = Field(default=2, ge=1, le=3)
    coordinator_url: str = "http://coordinator-worker:8090"
    specialist_url: str = "http://specialist-worker:8090"
    role_registry: RoleRegistry = Field(default_factory=default_role_registry)
    specialist_urls: dict[RoleId, str] = Field(default_factory=dict)
    discord_sre: DiscordSRE = Field(default_factory=DiscordSRE)
    workflow_v2: WorkflowV2 = Field(default_factory=WorkflowV2)
    upstream_url: str = "http://upstream-worker:8090"
    downstream_url: str = "http://downstream-worker:8090"

    @model_validator(mode="after")
    def live_ready(self):
        for role in self.specialist_urls:
            self.role_registry.resolve(role)
        if self.mode == "live":
            unknown_v2 = set(self.workflow_v2.repository_aliases) - set(self.repos)
            unknown_channels = set(self.workflow_v2.project_channels) - set(self.repos)
            if unknown_v2:
                raise ValueError(f"Unknown v2 repository aliases: {sorted(unknown_v2)}")
            if unknown_channels:
                raise ValueError(f"Unknown project channel aliases: {sorted(unknown_channels)}")
            if self.workflow_v2.enabled:
                approvers = {
                    *self.workflow_v2.requirements_approver_ids,
                    *self.workflow_v2.plan_approver_ids,
                }
                if not approvers or not all(value.isdigit() for value in approvers):
                    raise ValueError("v2 requires explicit Discord approval allowlists")
                if not approvers.issubset(self.owner_ids):
                    raise ValueError("v2 approvers must also be allowed owners")
                if not all(value.isdigit() for value in self.workflow_v2.project_channels.values()):
                    raise ValueError("v2 project channel IDs must be Discord snowflakes")
            if self.auth_mode == "api_key" and (
                not self.model
                or min(self.daily_budget_usd, self.task_budget_usd, self.run_reservation_usd) <= 0
            ):
                raise ValueError("API key mode requires model and positive daily/task/run reservations")
            if (
                not self.owner_ids
                or not all(x.isdigit() for x in self.owner_ids)
                or not self.guild_id.isdigit()
                or not self.channel_id.isdigit()
            ):
                raise ValueError("Set real Discord IDs")
            for repo in self.repos.values():
                if not repo.checks or repo.publisher_login == repo.reviewer_login:
                    raise ValueError("Trusted CI checks and separate GitHub actors are required")
            if self.natural_language_requests and (
                not self.message_content or self.default_repo not in self.repos
            ):
                raise ValueError("Natural-language requests require message content and a default repo")
            if self.discord_sre.enabled:
                ids = [
                    *self.discord_sre.managed_category_ids,
                    *self.discord_sre.protected_channel_ids,
                    *self.discord_sre.protected_role_ids,
                ]
                if self.discord_sre.audit_channel_id:
                    ids.append(self.discord_sre.audit_channel_id)
                if not self.discord_sre.managed_category_ids or not all(x.isdigit() for x in ids):
                    raise ValueError("Discord SRE requires real managed/protected Discord IDs")
                if self.channel_id not in self.discord_sre.protected_channel_ids:
                    raise ValueError("The intake channel must be protected from Discord SRE changes")
        return self

    def repo_for(self, task):
        configured = self.repos[task.repo]
        return configured.model_copy(
            update={"repository": task.data.get("repository", configured.repository)}
        )

    def specialist_endpoint(self, role: str) -> str:
        canonical = self.role_registry.resolve(role)
        definition = self.role_registry.role(canonical)
        alias_endpoint = next(
            (
                self.specialist_urls[alias]
                for alias in definition.aliases
                if alias in self.specialist_urls
            ),
            "",
        )
        return self.specialist_urls.get(
            role,
            self.specialist_urls.get(
                canonical,
                alias_endpoint or definition.worker_endpoint or self.specialist_url,
            ),
        )

    def role_enabled(self, role: str) -> bool:
        return self.role_registry.role(role).enabled

    def discord_role_enabled(self, role: str) -> bool:
        definition = self.role_registry.role(role)
        return definition.enabled and definition.discord_enabled

    def discord_bot_key(self, role: str) -> str:
        return self.role_registry.role(role).discord_bot_key


def load_settings() -> Settings:
    path = Path(os.environ.get("TEAM_CONFIG", "config.yaml"))
    values = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    for field in ("coordinator_url", "specialist_url", "upstream_url", "downstream_url"):
        if value := os.environ.get("TEAM_" + field.upper()):
            values[field] = value
    if value := os.environ.get("TEAM_SPECIALIST_URLS"):
        values["specialist_urls"] = json.loads(value)
    if value := os.environ.get("TEAM_WORKFLOW_V2_WORKER_URL"):
        values.setdefault("workflow_v2", {})["worker_url"] = value
    return Settings.model_validate(values)


def secret(name: str) -> str:
    return Path(os.environ.get(f"{name}_FILE", f"/run/secrets/{name.lower()}")).read_text().strip()
