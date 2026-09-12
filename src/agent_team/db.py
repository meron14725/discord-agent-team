"""Portable test DB; production uses PostgreSQL. All task mutations lock the task row."""

import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import JSON, Float, Integer, String, Text, UniqueConstraint, create_engine, select, text
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
        # Baseline v1 is additive; subsequent revisions must be explicit migrations.
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as c:
            c.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"))
            c.execute(text("INSERT INTO schema_migrations VALUES (1) ON CONFLICT DO NOTHING"))

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
