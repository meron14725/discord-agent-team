"""Portable test DB; production uses PostgreSQL. All task mutations lock the task row."""

import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def uid():
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    repo: Mapped[str] = mapped_column(String)
    thread_id: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="Clarifying")
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    spec_version: Mapped[int] = mapped_column(Integer, default=0)
    workflow_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    requirements_reference_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_plan_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Spec(Base):
    __tablename__ = "spec_versions"
    __table_args__ = (UniqueConstraint("task_id", "version"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(String)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class RecordMixin:
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Approval(RecordMixin, Base):
    __tablename__ = "approvals"


class Review(RecordMixin, Base):
    __tablename__ = "reviews"


class Run(RecordMixin, Base):
    __tablename__ = "agent_runs"


class Artifact(RecordMixin, Base):
    __tablename__ = "artifacts"


class Operation(RecordMixin, Base):
    __tablename__ = "external_operations"
    key: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String, default="pending")


class Event(RecordMixin, Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("source", "external_id"),)
    source: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)


class Outbox(RecordMixin, Base):
    __tablename__ = "outbox"
    sent: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(default=0)
    next_try: Mapped[float] = mapped_column(Float, default=0)


class Job(RecordMixin, Base):
    __tablename__ = "jobs"
    role: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str] = mapped_column(String, default="")
    lease: Mapped[float] = mapped_column(Float, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)


class RequirementsReference(Base):
    __tablename__ = "requirements_references"
    __table_args__ = (
        UniqueConstraint("repository", "issue_number"),
        CheckConstraint("issue_number > 0", name="ck_requirements_reference_issue_number"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    repository: Mapped[str] = mapped_column(String)
    issue_number: Mapped[int] = mapped_column(Integer)
    issue_url: Mapped[str] = mapped_column(String)
    body_hash: Mapped[str] = mapped_column(String)
    github_updated_at: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft", server_default="draft", index=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (
        UniqueConstraint("requirements_reference_id", "version"),
        CheckConstraint("version > 0", name="ck_plan_version_positive"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    requirements_reference_id: Mapped[str] = mapped_column(
        ForeignKey("requirements_references.id"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String)
    base_sha: Mapped[str] = mapped_column(String)
    review_status: Mapped[str] = mapped_column(
        String, default="pending", server_default="pending", index=True
    )
    created: Mapped[float] = mapped_column(Float, default=time.time)


class ApprovalGrant(Base):
    __tablename__ = "approval_grants"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    stage: Mapped[str] = mapped_column(String, index=True)
    target_hash: Mapped[str] = mapped_column(String)
    target_sha: Mapped[str] = mapped_column(String, default="", server_default="")
    confirmation_id: Mapped[str] = mapped_column(String, unique=True)
    actor_id: Mapped[str] = mapped_column(String)
    expires: Mapped[float | None] = mapped_column(Float, nullable=True)
    consumed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Consultation(Base):
    __tablename__ = "consultations"
    __table_args__ = (
        UniqueConstraint("task_id", "topic_id", "ordinal"),
        CheckConstraint("ordinal BETWEEN 1 AND 25", name="ck_consultation_topic_limit"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    topic_id: Mapped[str] = mapped_column(String, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    requester_role: Mapped[str] = mapped_column(String)
    consultant_role: Mapped[str] = mapped_column(String)
    question_summary: Mapped[str] = mapped_column(Text)
    conclusion_summary: Mapped[str] = mapped_column(Text)
    decision_criteria: Mapped[str] = mapped_column(Text, default="", server_default="")
    unresolved_summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    issue_comment_url: Mapped[str] = mapped_column(String, default="", server_default="")
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Delegation(Base):
    __tablename__ = "delegations"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    topic_id: Mapped[str] = mapped_column(String, index=True)
    source_role: Mapped[str] = mapped_column(String)
    target_role: Mapped[str] = mapped_column(String)
    purpose: Mapped[str] = mapped_column(Text)
    expected_artifact: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="pending", server_default="pending", index=True)
    content_hash: Mapped[str] = mapped_column(String)
    parent_delegation_id: Mapped[str | None] = mapped_column(
        ForeignKey("delegations.id"), nullable=True
    )
    request_message_id: Mapped[str] = mapped_column(String, default="", server_default="")
    acknowledgement_message_id: Mapped[str] = mapped_column(String, default="", server_default="")
    result_message_id: Mapped[str] = mapped_column(String, default="", server_default="")
    created: Mapped[float] = mapped_column(Float, default=time.time)


class ProjectWorkspace(Base):
    __tablename__ = "project_workspaces"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    repository: Mapped[str] = mapped_column(String, unique=True)
    discord_channel_id: Mapped[str] = mapped_column(String)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class TopicThread(Base):
    __tablename__ = "topic_threads"
    __table_args__ = (
        UniqueConstraint("task_id", "topic_id"),
        UniqueConstraint("task_id", "origin_event_id", "purpose_hash"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    topic_id: Mapped[str] = mapped_column(String)
    thread_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    origin_event_id: Mapped[str] = mapped_column(String)
    purpose_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="open", server_default="open", index=True)
    resolved_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    archive_after: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    issue_comment_url: Mapped[str] = mapped_column(String, default="", server_default="")
    created: Mapped[float] = mapped_column(Float, default=time.time)


class RepositoryLease(Base):
    __tablename__ = "repository_leases"
    __table_args__ = (CheckConstraint("fence >= 0", name="ck_repository_lease_fence"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    repository: Mapped[str] = mapped_column(String, unique=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    owner: Mapped[str] = mapped_column(String)
    fence: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    lease_expires: Mapped[float] = mapped_column(Float, default=0, server_default=text("0"), index=True)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class TaskProjection(Base):
    __tablename__ = "task_projections"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), unique=True)
    project_workspace_id: Mapped[str] = mapped_column(ForeignKey("project_workspaces.id"), index=True)
    status_message_id: Mapped[str] = mapped_column(String)
    projection_hash: Mapped[str] = mapped_column(String)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class ExecutionBudget(Base):
    __tablename__ = "execution_budgets"
    __table_args__ = (
        CheckConstraint(
            "model_reservations BETWEEN 0 AND 150", name="ck_execution_budget_model_limit"
        ),
        CheckConstraint(
            "consultation_reservations BETWEEN 0 AND 75",
            name="ck_execution_budget_consultation_limit",
        ),
        CheckConstraint(
            "plan_revision_reservations BETWEEN 0 AND 15",
            name="ck_execution_budget_plan_revision_limit",
        ),
        CheckConstraint(
            "implementation_revision_reservations BETWEEN 0 AND 15",
            name="ck_execution_budget_implementation_revision_limit",
        ),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), unique=True)
    model_reservations: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    consultation_reservations: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    plan_revision_reservations: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    implementation_revision_reservations: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    created: Mapped[float] = mapped_column(Float, default=time.time)


class DiscordChange(Base):
    __tablename__ = "discord_changes"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    event_id: Mapped[str] = mapped_column(String, unique=True)
    actor: Mapped[str] = mapped_column(String)
    guild_id: Mapped[str] = mapped_column(String)
    channel_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    digest: Mapped[str] = mapped_column(String)
    plan: Mapped[dict] = mapped_column(JSON)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    expires: Mapped[float] = mapped_column(Float)
    created: Mapped[float] = mapped_column(Float, default=time.time)


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(url, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.leader = None
        self.leader_mutex = threading.Lock()

    def migrate(self):
        from .migrations import migrate

        migrate(self.engine, Base.metadata)

    def acquire_leader(self):
        if self.engine.dialect.name != "postgresql":
            return
        self.leader = self.engine.connect()
        if not self.leader.execute(text("SELECT pg_try_advisory_lock(72918042)")).scalar():
            raise RuntimeError("Another orchestrator owns the database leader lock")
        self.leader.commit()

    def check_leader(self):
        with self.leader_mutex:
            if self.leader is not None:
                if self.leader.invalidated:
                    raise RuntimeError("Leader connection lost; restart required")
                self.leader.execute(text("SELECT 1"))
                self.leader.commit()

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as s:
            yield s


def task_lock(s, task_id):
    task = s.scalar(select(Task).where(Task.id == task_id).with_for_update())
    if task is None:
        raise ValueError("Unknown task")
    return task
