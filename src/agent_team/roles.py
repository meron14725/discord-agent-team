from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

RoleId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]
ParallelClass = Literal["coordinator", "read_only", "repository_write", "privileged"]


class RoleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: RoleId
    display_name: str = Field(min_length=1, max_length=100)
    responsibilities: list[str] = Field(min_length=1)
    capabilities: list[RoleId] = Field(min_length=1)
    tools: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(min_length=1)
    input_schema: str = Field(min_length=1, max_length=100)
    output_schema: str = Field(min_length=1, max_length=100)
    consultable_roles: list[RoleId] = Field(default_factory=list)
    parallel_class: ParallelClass
    fallback_role: RoleId | None = None
    discord_bot_key: RoleId
    worker_endpoint: str = ""
    enabled: bool = True
    discord_enabled: bool = True
    aliases: list[RoleId] = Field(default_factory=list)

    @field_validator(
        "responsibilities", "capabilities", "tools", "forbidden_actions", "consultable_roles", "aliases"
    )
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Role list values must be unique")
        if any(not value.strip() for value in values):
            raise ValueError("Role list values cannot be empty")
        return values

    @field_validator("worker_endpoint")
    @classmethod
    def endpoint_is_http(cls, value: str) -> str:
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("Worker endpoint must be an HTTP(S) URL")
        return value.rstrip("/")


class RoleRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: list[RoleDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def references_are_valid(self):
        ids = [role.id for role in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("Role IDs must be unique")

        aliases: dict[str, str] = {}
        canonical = set(ids)
        for role in self.entries:
            if role.id in role.aliases:
                raise ValueError(f"Role {role.id} cannot alias itself")
            for alias in role.aliases:
                if alias in canonical or alias in aliases:
                    raise ValueError(f"Role alias is not unique: {alias}")
                aliases[alias] = role.id

        by_id = {role.id: role for role in self.entries}
        known = canonical | set(aliases)
        for role in self.entries:
            unknown = set(role.consultable_roles) - known
            if unknown:
                raise ValueError(f"Role {role.id} consults unknown roles: {sorted(unknown)}")
            if role.id in {aliases.get(item, item) for item in role.consultable_roles}:
                raise ValueError(f"Role {role.id} cannot consult itself")
            if role.fallback_role is None:
                continue
            fallback_id = aliases.get(role.fallback_role, role.fallback_role)
            if fallback_id == role.id:
                raise ValueError(f"Role {role.id} cannot fall back to itself")
            fallback = by_id.get(fallback_id)
            if fallback is None:
                raise ValueError(f"Role {role.id} has an unknown fallback role")
            if not fallback.enabled:
                raise ValueError(f"Role {role.id} cannot fall back to a disabled role")
            if role.parallel_class == "privileged":
                raise ValueError("Privileged roles cannot use fallback roles")
            if fallback.parallel_class != role.parallel_class:
                raise ValueError("Fallback roles must use the same parallel class")
            if not set(role.capabilities).issubset(fallback.capabilities):
                raise ValueError("Fallback roles must provide every source capability")
        return self

    @property
    def aliases(self) -> dict[str, str]:
        return {alias: role.id for role in self.entries for alias in role.aliases}

    @property
    def role_ids(self) -> tuple[str, ...]:
        return tuple(role.id for role in self.entries)

    @property
    def enabled_role_ids(self) -> tuple[str, ...]:
        return tuple(role.id for role in self.entries if role.enabled)

    @property
    def disabled_role_ids(self) -> tuple[str, ...]:
        return tuple(role.id for role in self.entries if not role.enabled)

    @property
    def discord_enabled_role_ids(self) -> tuple[str, ...]:
        return tuple(role.id for role in self.entries if role.enabled and role.discord_enabled)

    def resolve(self, role_id: str) -> str:
        canonical = self.aliases.get(role_id, role_id)
        if canonical not in self.role_ids:
            raise ValueError(f"Unknown role: {role_id}")
        return canonical

    def role(self, role_id: str) -> RoleDefinition:
        canonical = self.resolve(role_id)
        return next(role for role in self.entries if role.id == canonical)


def default_role_registry() -> RoleRegistry:
    common_forbidden = [
        "read_or_share_secrets",
        "bypass_owner_approval",
        "publish_or_merge_directly",
    ]
    generic_endpoint = "http://specialist-worker:8090"
    return RoleRegistry(
        entries=[
            RoleDefinition(
                id="coordinator",
                display_name="統括・PM",
                responsibilities=["受付、担当選択、重複排除、優先順位、進行管理"],
                capabilities=["coordination", "task_routing"],
                tools=["conversation_context"],
                forbidden_actions=common_forbidden + ["approve_on_behalf_of_owner"],
                input_schema="CoordinationRequest",
                output_schema="CoordinationDecision",
                consultable_roles=[
                    "cto",
                    "backend_integrator",
                    "security_sre",
                    "frontend_ux",
                    "qa",
                    "evaluation_manager",
                    "analyst",
                ],
                parallel_class="coordinator",
                discord_bot_key="discord_coordinator_token",
                worker_endpoint="http://coordinator-worker:8090",
            ),
            RoleDefinition(
                id="cto",
                display_name="CTO・要件設計",
                responsibilities=["要件定義、設計、計画レビュー、独立コードレビュー"],
                capabilities=[
                    "requirements",
                    "architecture",
                    "planning",
                    "review",
                    "test_planning",
                    "acceptance_review",
                    "evaluation_design",
                    "evidence_review",
                    "research",
                    "analysis",
                ],
                tools=["repository_read", "issue_read", "plan_read"],
                forbidden_actions=common_forbidden + ["implement_reviewed_changes"],
                input_schema="SpecialistRequest",
                output_schema="SpecialistDecision",
                consultable_roles=[
                    "backend_integrator",
                    "security_sre",
                    "frontend_ux",
                    "qa",
                    "evaluation_manager",
                    "analyst",
                ],
                parallel_class="read_only",
                discord_bot_key="discord_upstream_token",
                worker_endpoint=generic_endpoint,
                aliases=["upstream"],
            ),
            RoleDefinition(
                id="backend_integrator",
                display_name="バックエンド・実装統合",
                responsibilities=["実装、デバッグ、テスト、変更統合"],
                capabilities=[
                    "implementation",
                    "debugging",
                    "testing",
                    "frontend_implementation",
                    "ux_review",
                ],
                tools=["repository_read", "patch_proposal", "approved_commands"],
                forbidden_actions=common_forbidden + ["change_approved_requirements"],
                input_schema="SpecialistRequest",
                output_schema="SpecialistDecision",
                consultable_roles=["cto", "security_sre", "frontend_ux", "qa"],
                parallel_class="repository_write",
                discord_bot_key="discord_downstream_token",
                worker_endpoint=generic_endpoint,
                aliases=["downstream"],
            ),
            RoleDefinition(
                id="security_sre",
                display_name="セキュリティ・SRE",
                responsibilities=["セキュリティ、実行基盤、監視、障害対応、安全な運用"],
                capabilities=["security_review", "operations", "privileged_change_plan"],
                tools=["platform_snapshot", "audit_read", "typed_change_plan"],
                forbidden_actions=common_forbidden + ["self_approve_privileged_change"],
                input_schema="SpecialistRequest",
                output_schema="SpecialistDecision",
                consultable_roles=["cto", "backend_integrator"],
                parallel_class="privileged",
                discord_bot_key="discord_sre_token",
                worker_endpoint=generic_endpoint,
                aliases=["sre"],
            ),
            RoleDefinition(
                id="frontend_ux",
                display_name="フロントエンド・UX",
                responsibilities=["画面実装、操作導線、アクセシビリティの検討"],
                capabilities=["frontend_implementation", "ux_review"],
                tools=["repository_read", "patch_proposal", "approved_commands"],
                forbidden_actions=common_forbidden + ["change_approved_requirements"],
                input_schema="SpecialistRequest",
                output_schema="SpecialistDecision",
                consultable_roles=["cto", "backend_integrator", "qa"],
                parallel_class="repository_write",
                fallback_role="backend_integrator",
                discord_bot_key="discord_frontend_ux_token",
                worker_endpoint=generic_endpoint,
                discord_enabled=False,
            ),
            RoleDefinition(
                id="qa",
                display_name="QA",
                responsibilities=["テスト設計、受け入れ条件の検証、回帰リスクの確認"],
                capabilities=["test_planning", "acceptance_review"],
                tools=["repository_read", "test_evidence_read"],
                forbidden_actions=common_forbidden + ["edit_implementation"],
                input_schema="SpecialistRequest",
                output_schema="SpecialistDecision",
                consultable_roles=["cto", "backend_integrator", "frontend_ux"],
                parallel_class="read_only",
                fallback_role="cto",
                discord_bot_key="discord_qa_token",
                worker_endpoint=generic_endpoint,
                discord_enabled=False,
            ),
            RoleDefinition(
                id="evaluation_manager",
                display_name="評価管理",
                responsibilities=["評価基準、証拠品質、AI成果の再現性を確認"],
                capabilities=["evaluation_design", "evidence_review"],
                tools=["repository_read", "test_evidence_read", "artifact_read"],
                forbidden_actions=common_forbidden + ["edit_implementation"],
                input_schema="SpecialistRequest",
                output_schema="SpecialistDecision",
                consultable_roles=["cto", "qa", "analyst"],
                parallel_class="read_only",
                fallback_role="cto",
                discord_bot_key="discord_evaluation_manager_token",
                worker_endpoint=generic_endpoint,
                discord_enabled=False,
            ),
            RoleDefinition(
                id="analyst",
                display_name="調査・分析",
                responsibilities=["調査、比較、前提と根拠の整理"],
                capabilities=["research", "analysis"],
                tools=["approved_sources", "artifact_read"],
                forbidden_actions=common_forbidden + ["edit_implementation"],
                input_schema="SpecialistRequest",
                output_schema="SpecialistDecision",
                consultable_roles=["cto", "evaluation_manager"],
                parallel_class="read_only",
                fallback_role="cto",
                discord_bot_key="discord_analyst_token",
                worker_endpoint=generic_endpoint,
                discord_enabled=False,
            ),
        ]
    )
