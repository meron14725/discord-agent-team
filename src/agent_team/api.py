import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .adapters.codex import MockRunner, RemoteRunner
from .adapters.github import GitHub, MockGitHub
from .config import load_settings, secret
from .consultations import ConsultationService
from .contracts import (
    CoordinationDecision,
    CoordinationMessage,
    DiscordPlatformSnapshot,
    DiscordSREPlan,
    DiscordTargetSnapshot,
    RunRequest,
    SpecialistRole,
)
from .coordination import validate_specialist_handoffs
from .db import (
    Artifact,
    Database,
    Delegation,
    Event,
    Job,
    Outbox,
    ProjectWorkspace,
    Task,
    TaskProjection,
    TopicThread,
    task_lock,
)
from .discord_sre import DiscordChangeService
from .engine import Engine
from .policy import GuardError
from .prompt_context import load_agent_prompt_context
from .redaction import SecretScanner
from .service import TaskService


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    event_id: str
    actor: str
    guild: str
    channel: str
    task_id: str = ""
    text: str = ""
    repo: str = ""
    version: int = 0
    hash: str = ""
    head_sha: str = ""
    base_sha: str = ""
    confirmation_id: str = ""
    bot: bool = False


class CoordinateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=100)
    actor: str
    guild: str
    channel: str
    text: str = Field(min_length=1, max_length=2000)
    history: list[str] = Field(default_factory=list, max_length=20)


class SpecialistCommand(CoordinateCommand):
    role: SpecialistRole
    instruction: str = Field(min_length=1, max_length=1500)
    discord_snapshot: DiscordPlatformSnapshot | None = None
    handoff_depth: Literal[0, 1, 2] = 0
    handoff_round: int = Field(default=0, ge=0, le=2)
    handoff_source_roles: list[SpecialistRole] = Field(default_factory=list, max_length=2)
    visited_roles: list[SpecialistRole] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def handoff_state_matches_depth(self):
        if self.handoff_depth == 0 and (self.handoff_round or self.handoff_source_roles):
            raise ValueError("Initial specialist turn cannot include handoff dialogue state")
        if self.handoff_depth == 1 and not self.handoff_source_roles:
            raise ValueError("Handoff recipient requires source roles")
        if self.handoff_depth == 2 and self.handoff_source_roles:
            raise ValueError("Handoff answer cannot include source roles")
        return self


class DiscordChangeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=100)
    actor: str
    guild: str
    channel: str
    plan: DiscordSREPlan
    before: DiscordTargetSnapshot | None = None


class DiscordChangeApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=100)
    actor: str
    guild: str
    channel: str
    digest: str


class DiscordChangeExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_before: DiscordTargetSnapshot | None = None


class DiscordChangeCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    success: bool
    result: dict = Field(default_factory=dict)
    error: str = Field(default="", max_length=1000)


class ConsultationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str
    guild: str
    channel: str
    task_id: str
    topic_id: str
    requester_role: SpecialistRole
    consultant_role: SpecialistRole
    question: str = Field(min_length=1, max_length=3000)


def explicitly_addresses_all(text: str) -> bool:
    compact = "".join(text.casefold().split())
    if "他のメンバーにも" in compact:
        return True
    for marker in ("みんな", "全員", "皆さん", "みなさん", "各メンバー"):
        if compact.startswith(marker) and not compact[len(marker) :].startswith(("とは", "という")):
            return True
    return False


def requests_discord_change(text: str) -> bool:
    compact = "".join(text.casefold().split())
    target = any(word in compact for word in ("チャンネル", "スレッド", "channel", "thread"))
    action = any(
        word in compact
        for word in (
            "作って",
            "作る",
            "作成",
            "変更して",
            "変更案",
            "更新して",
            "アーカイブして",
            "archive",
            "create",
            "update",
        )
    )
    informational = any(word in compact for word in ("作り方", "方法", "可能ですか", "できますか"))
    return target and action and not informational


def enforce_explicit_audience(
    decision: CoordinationDecision,
    text: str,
    roles: tuple[str, ...] = ("upstream", "downstream", "sre"),
) -> CoordinationDecision:
    if decision.action != "reply" or not explicitly_addresses_all(text):
        return decision
    return CoordinationDecision(
        action="delegate",
        reply=decision.reply,
        task_summary="",
        delegations=[
            CoordinationMessage(
                role=role,
                instruction=(
                    "オーナーからチーム全員への発言です。元の発言と会話文脈を踏まえ、"
                    "あなた自身の立場と言葉で自然に応答してください。作業を実行したとは主張しないでください。"
                ),
            )
            for role in roles
        ],
    )


def create_app(db=None, settings=None, token=None):
    settings = settings or load_settings()
    if db is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            from urllib.parse import quote

            url = f"postgresql+psycopg://team:{quote(secret('DB_PASSWORD'), safe='')}@postgres/team"
        db = Database(url)
    db.migrate()
    token = token or secret("INTERNAL_TOKEN")
    scanner = SecretScanner(hashlib.sha256(token.encode()).digest())
    prompt_context = load_agent_prompt_context(settings.role_registry)
    service = TaskService(db, settings)
    discord_changes = DiscordChangeService(db, settings)
    consultations = ConsultationService(db, settings)
    github = (
        MockGitHub(db, settings, scanner=scanner)
        if settings.mode == "mock"
        else GitHub(
            settings,
            {
                role: secret("GITHUB_" + role.upper() + "_TOKEN")
                for role in ("publisher", "reviewer", "merger")
                if role != "reviewer" or not settings.github_reviewer_app
            },
            reviewer_private_key=(
                secret("GITHUB_REVIEWER_PRIVATE_KEY") if settings.github_reviewer_app else ""
            ),
            scanner=scanner,
        )
    )
    runner = MockRunner() if settings.mode == "mock" else RemoteRunner(settings, secret("WORKER_TOKEN"))
    engine = Engine(db, settings, github, runner, os.environ.get("ARTIFACTS_DIR", "artifacts"))
    enabled_roles = {
        role.id: "、".join(role.responsibilities)
        for role in settings.role_registry.entries
        if role.enabled and role.discord_enabled
    }

    @asynccontextmanager
    async def lifespan(app):
        db.acquire_leader()
        future = asyncio.create_task(engine.loop())
        app.state.loop = future
        yield
        future.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await future
        for job_id, active in engine.active.items():
            active.cancel()
            await runner.cancel(job_id)
        if db.leader is not None:
            db.leader.close()

    app = FastAPI(title="Discord Agent Team (internal)", lifespan=lifespan)

    def authenticate(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Invalid internal credential")
        db.check_leader()

    @app.get("/health")
    def health():
        future = getattr(app.state, "loop", None)
        if future is not None and future.done():
            raise HTTPException(503, "Orchestrator loop stopped")
        db.check_leader()
        return {"status": "ok", "mode": settings.mode}

    @app.post("/commands", dependencies=[Depends(authenticate)])
    async def command(command: Command):
        try:
            if scanner.scan_text(command.text).blocked:
                raise GuardError("Potential secret blocked at Discord input boundary")
            result = service.command(**command.model_dump())
            if command.action == "cancel":
                await engine.cancel_task(result["id"])
            return result
        except GuardError as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/coordinate", dependencies=[Depends(authenticate)])
    async def coordinate(command: CoordinateCommand):
        try:
            if scanner.scan_text(command.text + "\n" + "\n".join(command.history)).blocked:
                raise GuardError("Potential secret blocked at Discord input boundary")
            service.authorize(command.actor, command.guild, command.channel)
            if sum(len(item) for item in command.history) > 30_000:
                raise GuardError("Conversation context is too large")
            request = RunRequest(
                job_id="coord-" + command.event_id,
                role="coordinator",
                kind="coordinate",
                task_id="COORD-" + command.event_id[-20:],
                spec_version=0,
                spec_hash="",
                base_sha="",
                head_sha="",
                prompt=json.dumps(
                    {
                        "current_owner_message": command.text,
                        "recent_discord_context_oldest_first": command.history,
                        "available_roles": enabled_roles,
                        "default_repository_alias": settings.default_repo,
                        "trusted_company_memory": prompt_context.company_memory,
                        "trusted_company_policy": prompt_context.company_policy,
                        "trusted_role_policies": prompt_context.role_policies,
                    },
                    ensure_ascii=False,
                ),
                files={},
                test_commands=[],
                model=settings.model,
                timeout=settings.coordination_timeout,
            )
            response = await runner.run(request)
            audience = tuple(
                role.id
                for role in settings.role_registry.entries
                if role.enabled and role.discord_enabled and role.id != "coordinator"
            )
            decision = enforce_explicit_audience(
                response.result.coordination,
                command.text,
                audience,
            )
            if not explicitly_addresses_all(command.text) and len(decision.delegations) > 4:
                raise GuardError("Coordinator may select at most four specialists")
            for delegation in decision.delegations:
                role = settings.role_registry.role(delegation.role)
                if not role.enabled or not role.discord_enabled or role.id == "coordinator":
                    raise GuardError("Coordinator selected an unavailable specialist")
            return decision
        except GuardError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            raise HTTPException(503, "Coordinator agent is temporarily unavailable") from error

    @app.post("/specialist-turn", dependencies=[Depends(authenticate)])
    async def specialist_turn(command: SpecialistCommand):
        try:
            if scanner.scan_text(
                command.text + "\n" + command.instruction + "\n" + "\n".join(command.history)
            ).blocked:
                raise GuardError("Potential secret blocked at Discord input boundary")
            from collections import Counter

            service.authorize(command.actor, command.guild, command.channel)
            if sum(len(item) for item in command.history) > 30_000:
                raise GuardError("Conversation context is too large")
            snapshot = {}
            canonical_role = settings.role_registry.resolve(command.role)
            role_definition = settings.role_registry.role(canonical_role)
            if (
                not role_definition.enabled
                or not role_definition.discord_enabled
                or canonical_role == "coordinator"
            ):
                raise GuardError("Specialist role is unavailable")
            if canonical_role == "security_sre":
                if command.discord_snapshot and command.discord_snapshot.guild_id != command.guild:
                    raise GuardError("Discord snapshot guild mismatch")
                with db.transaction() as session:
                    snapshot = {
                        "orchestrator_mode": settings.mode,
                        "merge_mode": settings.merge_mode,
                        "repository_aliases": sorted(settings.repos),
                        "task_states": dict(Counter(session.scalars(select(Task.state)))),
                        "job_states": dict(Counter(session.scalars(select(Job.status)))),
                        "recent_state_changes": [
                            {"task_id": event.task_id, **event.data}
                            for event in session.scalars(
                                select(Event)
                                .where(Event.source == "state")
                                .order_by(Event.created.desc())
                                .limit(10)
                            )
                        ],
                        "discord": (
                            command.discord_snapshot.model_dump(mode="json")
                            if command.discord_snapshot
                            else None
                        ),
                    }
            elif command.discord_snapshot is not None:
                raise GuardError("Discord snapshot is restricted to the SRE role")
            request = RunRequest(
                job_id=f"respond-{command.role}-{command.event_id}",
                role=command.role,
                kind="respond",
                task_id=f"CHAT-{command.role}-{command.event_id[-20:]}",
                spec_version=0,
                spec_hash="",
                base_sha="",
                head_sha="",
                prompt=json.dumps(
                    {
                        "current_owner_message": command.text,
                        "recent_discord_context_oldest_first": command.history,
                        "delegated_goal": command.instruction,
                        "handoff_policy": {
                            "mode": {0: "initial", 1: "recipient", 2: "answer"}[
                                command.handoff_depth
                            ],
                            "round_trips_used": command.handoff_round,
                            "round_trips_remaining": (
                                2 - command.handoff_round if command.handoff_depth == 1 else 0
                            ),
                            "allowed": command.handoff_depth == 0
                            or (command.handoff_depth == 1 and command.handoff_round < 2),
                            "allowed_target_roles": (
                                command.handoff_source_roles if command.handoff_depth == 1 else []
                            ),
                            "visited_roles": sorted(set(command.visited_roles) | {command.role}),
                            "maximum_targets": 2,
                        },
                        "available_roles": enabled_roles,
                        "discord_change_plan_required": (
                            canonical_role == "security_sre"
                            and command.handoff_depth != 2
                            and requests_discord_change(command.text)
                        ),
                        "trusted_platform_snapshot": snapshot,
                        "trusted_company_memory": prompt_context.company_memory,
                        "trusted_company_policy": prompt_context.company_policy,
                        "trusted_role_policy": prompt_context.role_policies[canonical_role],
                    },
                    ensure_ascii=False,
                ),
                files={},
                test_commands=[],
                model=settings.model,
                timeout=settings.coordination_timeout,
            )
            response = await runner.run(request)
            decision = response.result.specialist
            plan_required = (
                canonical_role == "security_sre"
                and command.handoff_depth != 2
                and requests_discord_change(command.text)
            )
            if plan_required and (
                decision.action != "request_approval" or decision.sre_plan is None
            ):
                retry_payload = json.loads(request.prompt)
                retry_payload["validation_feedback"] = (
                    "The explicit Discord change request requires action=request_approval and a non-null "
                    "sre_plan. Return one supported typed operation; do not return a prose-only proposal."
                )
                retry_request = request.model_copy(
                    update={
                        "job_id": request.job_id + "-typed-retry",
                        "prompt": json.dumps(retry_payload, ensure_ascii=False),
                    }
                )
                response = await runner.run(retry_request)
                decision = response.result.specialist
            if plan_required and (
                decision.action != "request_approval" or decision.sre_plan is None
            ):
                raise GuardError("SRE omitted the required typed Discord change plan")
            if decision.sre_plan is not None:
                if canonical_role != "security_sre":
                    raise GuardError("Discord SRE plans are restricted to the SRE role")
                if command.handoff_depth == 2:
                    raise GuardError("Internal handoff answers cannot propose Discord changes")
                if not requests_discord_change(command.text):
                    raise GuardError("Discord SRE changes require an explicit owner request")
                if decision.sre_plan.guild_id != command.guild:
                    raise GuardError("Discord SRE plan guild mismatch")
            allowed_targets = {
                settings.role_registry.resolve(role) for role in role_definition.consultable_roles
            }
            for handoff in decision.handoffs:
                target = settings.role_registry.role(handoff.role)
                if (
                    not target.enabled
                    or not target.discord_enabled
                    or target.id not in allowed_targets
                ):
                    raise GuardError("Specialist selected a disallowed handoff target")
            return validate_specialist_handoffs(
                decision,
                source_role=command.role,
                handoff_depth=command.handoff_depth,
                handoff_round=command.handoff_round,
                handoff_source_roles=command.handoff_source_roles,
                visited_roles=command.visited_roles,
            )
        except GuardError as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            raise HTTPException(503, "Specialist agent is temporarily unavailable") from error

    @app.post("/discord-changes", dependencies=[Depends(authenticate)])
    def propose_discord_change(command: DiscordChangeProposal):
        try:
            return discord_changes.propose(**command.model_dump())
        except GuardError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/consultations", dependencies=[Depends(authenticate)])
    def request_consultation(command: ConsultationCommand):
        try:
            if scanner.scan_text(command.question).blocked:
                raise GuardError("Potential secret blocked at consultation boundary")
            with db.transaction() as session:
                task = task_lock(session, command.task_id)
                service.authorize(
                    command.actor,
                    command.guild,
                    command.channel,
                    task,
                )
            consultation_id = consultations.request(
                command.task_id,
                topic_id=command.topic_id,
                requester_role=command.requester_role,
                consultant_role=command.consultant_role,
                question=command.question,
            )
            return {"consultation_id": consultation_id, "status": "queued"}
        except GuardError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/discord-changes/{change_id}/approve", dependencies=[Depends(authenticate)])
    def approve_discord_change(change_id: str, command: DiscordChangeApproval):
        try:
            return discord_changes.approve(change_id, **command.model_dump())
        except GuardError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/discord-changes/{change_id}/authorize", dependencies=[Depends(authenticate)])
    def authorize_discord_change(change_id: str, command: DiscordChangeExecution):
        try:
            return discord_changes.authorize_execution(change_id, command.current_before)
        except GuardError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.post("/discord-changes/{change_id}/complete", dependencies=[Depends(authenticate)])
    def complete_discord_change(change_id: str, command: DiscordChangeCompletion):
        try:
            return discord_changes.complete(change_id, **command.model_dump())
        except GuardError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/outbox", dependencies=[Depends(authenticate)])
    def outbox():
        import time

        with db.transaction() as s:
            items = []
            for out in s.scalars(
                select(Outbox)
                .where(Outbox.sent.is_(False), Outbox.next_try <= time.time())
                .order_by(Outbox.created)
                .limit(30)
            ):
                item = {"id": out.id, "task_id": out.task_id, **out.data}
                if out.data.get("project_status"):
                    projection = s.scalar(
                        select(TaskProjection).where(TaskProjection.task_id == out.task_id)
                    )
                    item["status_message_id"] = (
                        projection.status_message_id if projection else ""
                    )
                if artifact_ids := out.data.get("artifact_ids"):
                    attachments = []
                    for artifact_id in artifact_ids:
                        artifact = s.get(Artifact, artifact_id)
                        if artifact is None or artifact.task_id != out.task_id:
                            raise HTTPException(409, "Explanation artifact is unavailable")
                        for kind in ("html", "png"):
                            path = artifact.data.get(f"{kind}_path", "")
                            content = Path(path).read_bytes()
                            if len(content) > settings.max_bytes:
                                raise HTTPException(409, "Explanation artifact is too large")
                            attachments.append(
                                {
                                    "filename": f"{artifact.data['source_kind']}.{kind}",
                                    "content_base64": base64.b64encode(content).decode("ascii"),
                                }
                            )
                    item["attachments"] = attachments
                items.append(item)
            return items

    @app.post("/outbox/{message_id}/ack", dependencies=[Depends(authenticate)])
    def ack(message_id: str, payload: dict):
        with db.transaction() as s:
            out = s.get(Outbox, message_id)
            if out is None:
                raise HTTPException(404)
            out.sent = True
            out.data = {**out.data, "message_id": str(payload["message_id"])}
            if out.data.get("project_status"):
                task = task_lock(s, out.task_id)
                workspace = s.scalar(
                    select(ProjectWorkspace).where(
                        ProjectWorkspace.repository == task.data["repository"]
                    )
                )
                projection = s.scalar(
                    select(TaskProjection).where(TaskProjection.task_id == task.id)
                )
                if projection:
                    projection.status_message_id = str(payload["message_id"])
                    projection.projection_hash = out.data.get("projection_hash", "")
                else:
                    s.add(
                        TaskProjection(
                            task_id=task.id,
                            project_workspace_id=workspace.id,
                            status_message_id=str(payload["message_id"]),
                            projection_hash=out.data.get("projection_hash", ""),
                        )
                    )
            if out.data.get("create_topic_thread"):
                topic = s.get(TopicThread, out.data["topic_record_id"])
                if topic is None:
                    raise HTTPException(404)
                topic.thread_id = str(payload["thread_id"])
                task = task_lock(s, out.task_id)
                if not task.thread_id:
                    task.thread_id = topic.thread_id
                task.data = {
                    **task.data,
                    "topic_thread_ids": sorted(
                        {*task.data.get("topic_thread_ids", []), topic.thread_id}
                    ),
                }
            if out.data.get("archive_topic_thread"):
                topic = s.get(TopicThread, out.data["topic_record_id"])
                if topic is None:
                    raise HTTPException(404)
                topic.status = "archived"
            if out.data.get("delegation_log"):
                delegation = s.get(Delegation, out.data["delegation_id"])
                if delegation:
                    delegation.request_message_id = str(payload["message_id"])
            if out.data.get("create_thread"):
                task = task_lock(s, out.task_id)
                task.thread_id = str(payload["thread_id"])
                for other in s.scalars(
                    select(Outbox).where(Outbox.task_id == task.id, Outbox.sent.is_(False))
                ):
                    other.data = {**other.data, "thread_id": task.thread_id}
        return {"ok": True}

    @app.post("/outbox/{message_id}/fail", dependencies=[Depends(authenticate)])
    def failed(message_id: str):
        import time

        with db.transaction() as s:
            out = s.get(Outbox, message_id)
            if out:
                out.attempts += 1
                out.next_try = time.time() + min(300, 2 ** min(out.attempts, 8))
        return {"ok": True}

    @app.post("/buttons/{message_id}", dependencies=[Depends(authenticate)])
    def button(message_id: str, command: Command):
        with db.transaction() as s:
            out = s.get(Outbox, message_id)
            if out is None or not out.data.get("approval"):
                raise HTTPException(404)
            command.task_id = out.task_id
            command.action = "approve_" + out.data["approval"]
            for name in ("version", "hash", "head_sha", "base_sha", "confirmation_id"):
                if name in out.data:
                    setattr(command, name, out.data[name])
        return globals_command(command)

    globals_command = command

    @app.get("/threads", dependencies=[Depends(authenticate)])
    def threads():
        with db.transaction() as s:
            legacy = [
                {"task_id": t.id, "thread_id": t.thread_id, "state": t.state}
                for t in s.scalars(
                    select(Task).where(
                        Task.thread_id != "", Task.state.in_(["Clarifying", "AwaitingSpecApproval"])
                    )
                )
            ]
            topics = [
                {
                    "task_id": topic.task_id,
                    "thread_id": topic.thread_id,
                    "state": "TopicOpen",
                    "topic_id": topic.topic_id,
                }
                for topic in s.scalars(
                    select(TopicThread).where(
                        TopicThread.thread_id.is_not(None), TopicThread.status == "open"
                    )
                )
            ]
            return legacy + topics

    @app.get("/metrics", dependencies=[Depends(authenticate)])
    def metrics():
        from collections import Counter

        with db.transaction() as s:
            return {
                "tasks": dict(Counter(s.scalars(select(Task.state)))),
                "jobs": dict(Counter(s.scalars(select(Job.status)))),
            }

    return app
