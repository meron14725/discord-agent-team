from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


SpecialistRole = Literal["upstream", "downstream", "sre"]


class Finding(Strict):
    id: str
    severity: Literal["critical", "high", "medium", "low"]
    requirement_id: str
    file: str
    line: int = Field(ge=1)
    reason: str
    requested_change: str


class Coverage(Strict):
    acceptance_id: str
    status: Literal["met", "not_met", "unknown"]
    evidence: str


class CoordinationMessage(Strict):
    role: SpecialistRole
    instruction: str = Field(
        min_length=1,
        max_length=1500,
        description="Goal and context delegated to this specialist; the specialist decides its response",
    )


class SpecialistHandoff(Strict):
    role: SpecialistRole
    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Why this other specialist is needed",
    )
    instruction: str = Field(
        min_length=1,
        max_length=1500,
        description="A self-contained goal for the receiving specialist",
    )


class SpecialistDecision(Strict):
    action: Literal["reply", "clarify", "recommend_task", "request_approval", "handoff"]
    reply: str = Field(min_length=1, max_length=1500)
    task_summary: str = Field(
        max_length=2000,
        description="Required for recommend_task; ignored and normalized to empty otherwise",
    )
    approval_reason: str = Field(
        max_length=1500,
        description="Required for request_approval; ignored and normalized to empty otherwise",
    )
    sre_plan: "DiscordSREPlan | None"
    handoffs: list[SpecialistHandoff] = Field(max_length=2)

    @model_validator(mode="after")
    def action_payload_matches(self):
        if self.action == "recommend_task" and not self.task_summary.strip():
            raise ValueError("Task recommendations require a task summary")
        if self.action != "recommend_task":
            self.task_summary = ""
        if self.action == "request_approval" and not self.approval_reason.strip():
            raise ValueError("Approval requests require a reason")
        if self.action != "request_approval":
            self.approval_reason = ""
        if self.sre_plan is not None and self.action != "request_approval":
            raise ValueError("Discord SRE plans require an approval request")
        if (self.action == "handoff") != bool(self.handoffs):
            raise ValueError("Only handoff decisions may include handoffs")
        if len({item.role for item in self.handoffs}) != len(self.handoffs):
            raise ValueError("A role may receive at most one specialist handoff")
        return self


class DiscordTargetSnapshot(Strict):
    guild_id: str
    target_id: str
    kind: Literal["category", "text", "thread"]
    name: str
    parent_id: str = ""
    category_id: str = ""
    topic: str = ""
    archived: bool = False


class DiscordPermissionSnapshot(Strict):
    administrator: bool
    view_audit_log: bool
    manage_guild: bool
    manage_channels: bool
    manage_roles: bool
    manage_messages: bool
    manage_threads: bool
    create_public_threads: bool
    create_private_threads: bool


class DiscordRoleSnapshot(Strict):
    id: str
    name: str = Field(max_length=100)
    position: int = Field(ge=0)
    managed: bool
    protected: bool


class DiscordChannelSummary(Strict):
    id: str
    name: str = Field(max_length=100)
    kind: Literal["category", "text", "voice", "thread", "other"]
    category_id: str = ""
    topic: str = Field(default="", max_length=1024)
    protected: bool
    managed: bool


class DiscordAuditSummary(Strict):
    action: str = Field(min_length=1, max_length=100)
    target_id: str = ""
    actor_id: str = ""
    reason: str = Field(default="", max_length=500)
    created_at: str


class DiscordPlatformSnapshot(Strict):
    guild_id: str
    bot_id: str
    permissions: DiscordPermissionSnapshot
    roles: list[DiscordRoleSnapshot] = Field(max_length=100)
    channels: list[DiscordChannelSummary] = Field(max_length=500)
    recent_audit: list[DiscordAuditSummary] = Field(max_length=20)


class DiscordSREPlan(Strict):
    schema_version: Literal[1]
    operation: Literal["create_text_channel", "update_channel_topic", "archive_thread"]
    guild_id: str
    target_id: str
    parent_category_id: str
    name: str
    topic: str
    archive: bool | None
    reason: str = Field(min_length=1, max_length=1000)
    impact: str = Field(min_length=1, max_length=1000)
    verification: str = Field(min_length=1, max_length=1000)
    rollback: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def operation_payload_matches(self):
        if self.operation == "create_text_channel":
            if not self.parent_category_id or not self.name or self.target_id or self.archive is not None:
                raise ValueError("create_text_channel requires parent_category_id and name only")
        elif self.operation == "update_channel_topic":
            if not self.target_id or self.parent_category_id or self.name or self.archive is not None:
                raise ValueError("update_channel_topic requires target_id and topic only")
        elif self.operation == "archive_thread":
            if not self.target_id or self.parent_category_id or self.name or self.topic or self.archive is not True:
                raise ValueError("archive_thread requires target_id and archive=true only")
        return self


class CoordinationDecision(Strict):
    action: Literal["reply", "delegate", "task", "clarify"]
    reply: str = Field(min_length=1, max_length=1500)
    task_summary: str = Field(max_length=2000)
    delegations: list[CoordinationMessage] = Field(max_length=3)

    @model_validator(mode="after")
    def action_payload_matches(self):
        if self.action == "task" and not self.task_summary.strip():
            raise ValueError("Task decisions require a summary")
        if self.action == "delegate" and not self.delegations:
            raise ValueError("Delegation decisions require messages")
        if self.action != "task" and self.task_summary:
            raise ValueError("Only task decisions may include a task summary")
        if self.action != "delegate" and self.delegations:
            raise ValueError("Only delegation decisions may include delegated messages")
        if len({item.role for item in self.delegations}) != len(self.delegations):
            raise ValueError("A role may receive at most one delegated message")
        return self


class Result(Strict):
    schema_version: Literal[1]
    task_id: str
    spec_version: int
    spec_hash: str
    head_sha: str
    base_sha: str
    status: Literal["completed", "needs_clarification", "blocked", "failed"]
    summary: str
    spec_markdown: str
    questions: list[str]
    decision: Literal["approve", "request_changes", "needs_human", "none"]
    findings: list[Finding]
    coverage: list[Coverage]
    plan: str
    risks: list[str]
    coordination: CoordinationDecision | None
    specialist: SpecialistDecision | None
    # Files and test results are collected by the runner, never trusted from LLM prose.


class RunRequest(Strict):
    auth_mode: Literal["chatgpt", "api_key"] = "chatgpt"
    job_id: str
    role: Literal["coordinator", "upstream", "downstream", "sre"]
    kind: Literal["coordinate", "respond", "clarify", "implement", "fix", "review"]
    task_id: str
    spec_version: int
    spec_hash: str
    base_sha: str
    head_sha: str
    prompt: str
    files: dict[str, str]
    test_commands: list[list[str]]
    model: str
    timeout: int


class TestEvidence(Strict):
    command: list[str]
    exit_code: int
    output: str


class RunResponse(Strict):
    result: Result
    files: dict[str, str | None]
    tests: list[TestEvidence]
    usage: dict[str, int]
    cli_version: str
    elapsed_seconds: float
