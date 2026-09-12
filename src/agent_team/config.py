import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class Check(BaseModel):
    name: str
    app_id: int


class Repo(BaseModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base: str = "master"
    per_task: bool = False
    private: bool = True
    template_repository: str = ""
    publisher_login: str = "agent-publisher[bot]"
    reviewer_login: str = "agent-reviewer[bot]"
    checks: list[Check] = Field(default_factory=list)
    test_commands: list[list[str]] = Field(default_factory=lambda: [["python", "-m", "unittest", "discover"]])
    allowed_paths: list[str] = Field(default_factory=lambda: ["src/*", "tests/*", "docs/tasks/*"])


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


class Settings(BaseModel):
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
    daily_run_limit: int = Field(default=30, ge=1)
    task_run_limit: int = Field(default=10, ge=1)
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
    specialist_concurrency: int = Field(default=1, ge=1, le=3)
    specialist_retry_attempts: int = Field(default=1, ge=1, le=2)
    coordinator_url: str = "http://coordinator-worker:8090"
    specialist_url: str = "http://specialist-worker:8090"
    specialist_urls: dict[Literal["upstream", "downstream", "sre"], str] = Field(
        default_factory=dict
    )
    discord_sre: DiscordSRE = Field(default_factory=DiscordSRE)
    upstream_url: str = "http://upstream-worker:8090"
    downstream_url: str = "http://downstream-worker:8090"

    @model_validator(mode="after")
    def live_ready(self):
        if self.mode == "live":
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
        return self.specialist_urls.get(role, self.specialist_url)


def load_settings() -> Settings:
    path = Path(os.environ.get("TEAM_CONFIG", "config.yaml"))
    values = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    for field in ("coordinator_url", "specialist_url", "upstream_url", "downstream_url"):
        if value := os.environ.get("TEAM_" + field.upper()):
            values[field] = value
    if value := os.environ.get("TEAM_SPECIALIST_URLS"):
        values["specialist_urls"] = json.loads(value)
    return Settings.model_validate(values)


def secret(name: str) -> str:
    return Path(os.environ.get(f"{name}_FILE", f"/run/secrets/{name.lower()}")).read_text().strip()
