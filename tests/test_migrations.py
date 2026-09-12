import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

from agent_team.db import (
    ApprovalGrant,
    Base,
    Consultation,
    Database,
    ExecutionBudget,
    Job,
    PlanVersion,
    ProjectWorkspace,
    RepositoryLease,
    RequirementsReference,
    Task,
    TaskProjection,
    TopicThread,
)
from agent_team.migrations import V2_TABLES


def create_v1_database(path):
    engine = create_engine("sqlite:///" + str(path))
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE tasks (
                    id VARCHAR PRIMARY KEY,
                    repo VARCHAR NOT NULL,
                    thread_id VARCHAR NOT NULL,
                    state VARCHAR NOT NULL,
                    state_version INTEGER NOT NULL,
                    spec_version INTEGER NOT NULL,
                    data JSON NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks
                    (id, repo, thread_id, state, state_version, spec_version, data)
                VALUES
                    ('TASK-LEGACY', 'demo', '', 'Clarifying', 0, 0, '{}')
                """
            )
        )
        connection.execute(text("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO schema_migrations VALUES (1)"))
    engine.dispose()


def add_task(session, task_id):
    task = Task(id=task_id, repo="demo", data={})
    session.add(task)
    session.flush()
    return task


def add_requirements_reference(session, task, reference_id="REQ-1"):
    reference = RequirementsReference(
        id=reference_id,
        task_id=task.id,
        repository="example/demo",
        issue_number=2,
        issue_url="https://github.example/example/demo/issues/2",
        body_hash="sha256:requirements",
        github_updated_at="2026-09-12T13:53:43Z",
    )
    session.add(reference)
    session.flush()
    return reference


def test_v2_migration_preserves_v1_rows_and_defaults_them_to_workflow_one(tmp_path):
    path = tmp_path / "legacy.db"
    create_v1_database(path)
    db = Database("sqlite:///" + str(path))

    db.migrate()

    with db.transaction() as session:
        legacy = session.get(Task, "TASK-LEGACY")
        assert legacy.workflow_version == 1
        assert legacy.requirements_reference_id is None
        assert legacy.current_plan_version_id is None
        assert session.execute(text("SELECT version FROM schema_migrations ORDER BY version")).scalars().all() == [
            1,
            2,
        ]
    with db.engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        assert {
            "requirements_references",
            "plan_versions",
            "approval_grants",
            "consultations",
            "delegations",
            "project_workspaces",
            "topic_threads",
            "repository_leases",
            "task_projections",
            "execution_budgets",
        } <= table_names


def test_v2_migration_is_idempotent_and_database_default_remains_v1(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "team.db"))
    db.migrate()
    db.migrate()

    with db.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks
                    (id, repo, thread_id, state, state_version, spec_version, data)
                VALUES
                    ('TASK-RAW', 'demo', '', 'Clarifying', 0, 0, '{}')
                """
            )
        )
        versions = connection.execute(
            text("SELECT version, COUNT(*) FROM schema_migrations GROUP BY version ORDER BY version")
        ).all()
        assert versions == [(1, 1), (2, 1)]
    with db.transaction() as session:
        assert session.get(Task, "TASK-RAW").workflow_version == 1


def test_failed_v2_migration_does_not_record_a_partial_schema_version(tmp_path, monkeypatch):
    from agent_team import migrations

    path = tmp_path / "legacy.db"
    create_v1_database(path)
    original_create_tables = migrations._create_tables

    def fail_before_v2_tables(connection, metadata, names):
        if tuple(names) == migrations.V2_TABLES:
            raise RuntimeError("injected migration failure")
        original_create_tables(connection, metadata, names)

    monkeypatch.setattr(migrations, "_create_tables", fail_before_v2_tables)
    db = Database("sqlite:///" + str(path))
    with pytest.raises(RuntimeError, match="injected migration failure"):
        db.migrate()

    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version FROM schema_migrations ORDER BY version")
        ).scalars().all() == [1]


def test_v2_table_ddl_compiles_for_postgresql():
    statements = [
        str(CreateTable(Base.metadata.tables[name]).compile(dialect=postgresql.dialect()))
        for name in V2_TABLES
    ]

    assert len(statements) == len(V2_TABLES)
    assert all("CREATE TABLE" in statement for statement in statements)


def test_issue_plan_confirmation_and_consultation_identities_are_unique(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "team.db"))
    db.migrate()
    with db.transaction() as session:
        first = add_task(session, "TASK-1")
        add_task(session, "TASK-2")
        reference = add_requirements_reference(session, first)
        session.add(
            PlanVersion(
                task_id=first.id,
                requirements_reference_id=reference.id,
                version=1,
                path="docs/work-items/issue-2/plans/v1.md",
                content_hash="sha256:plan-1",
                base_sha="a" * 40,
            )
        )
        session.add(
            ApprovalGrant(
                task_id=first.id,
                stage="requirements",
                target_hash=reference.body_hash,
                confirmation_id="CONFIRM-1",
                actor_id="owner",
            )
        )
        session.add(
            Consultation(
                task_id=first.id,
                topic_id="TOPIC-1",
                ordinal=1,
                requester_role="cto",
                consultant_role="analyst",
                question_summary="市場判断の確認",
                conclusion_summary="選択肢Aを推奨",
            )
        )
        session.flush()

    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(
            RequirementsReference(
                task_id="TASK-2",
                repository="example/demo",
                issue_number=2,
                issue_url="https://github.example/example/demo/issues/2",
                body_hash="sha256:new-body",
                github_updated_at="2026-09-12T14:00:00Z",
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(
            PlanVersion(
                task_id="TASK-2",
                requirements_reference_id="REQ-1",
                version=1,
                path="docs/work-items/issue-2/plans/v1-copy.md",
                content_hash="sha256:duplicate-plan-version",
                base_sha="b" * 40,
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(
            ApprovalGrant(
                task_id="TASK-2",
                stage="plan",
                target_hash="sha256:plan-1",
                confirmation_id="CONFIRM-1",
                actor_id="owner",
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(
            Consultation(
                task_id="TASK-1",
                topic_id="TOPIC-1",
                ordinal=1,
                requester_role="cto",
                consultant_role="analyst",
                question_summary="duplicate",
                conclusion_summary="duplicate",
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(
            Consultation(
                task_id="TASK-1",
                topic_id="TOPIC-1",
                ordinal=6,
                requester_role="cto",
                consultant_role="analyst",
                question_summary="over limit",
                conclusion_summary="over limit",
            )
        )


def test_workspace_topic_lease_projection_and_budget_constraints(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "team.db"))
    db.migrate()
    with db.transaction() as session:
        task = add_task(session, "TASK-1")
        other = add_task(session, "TASK-2")
        first_job = Job(task_id=task.id, role="downstream", kind="implement", data={})
        second_job = Job(task_id=other.id, role="downstream", kind="implement", data={})
        workspace = ProjectWorkspace(
            repository="example/demo", discord_channel_id="CHANNEL-1"
        )
        session.add_all([first_job, second_job, workspace])
        session.flush()
        session.add_all(
            [
                TopicThread(
                    task_id=task.id,
                    topic_id="TOPIC-1",
                    origin_event_id="EVENT-1",
                    purpose_hash="sha256:purpose",
                ),
                RepositoryLease(
                    repository="example/demo",
                    job_id=first_job.id,
                    owner="worker-1",
                    fence=1,
                    lease_expires=100,
                ),
                TaskProjection(
                    task_id=task.id,
                    project_workspace_id=workspace.id,
                    status_message_id="MESSAGE-1",
                    projection_hash="sha256:projection",
                ),
                ExecutionBudget(task_id=task.id),
            ]
        )
        session.flush()

    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(ProjectWorkspace(repository="example/demo", discord_channel_id="CHANNEL-2"))
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(
            TopicThread(
                task_id="TASK-1",
                topic_id="RENAMED-TOPIC",
                origin_event_id="EVENT-1",
                purpose_hash="sha256:purpose",
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as session:
        second_job_id = session.scalar(select(Job.id).where(Job.task_id == "TASK-2"))
        session.add(
            RepositoryLease(
                repository="example/demo",
                job_id=second_job_id,
                owner="worker-2",
                fence=1,
                lease_expires=100,
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as session:
        workspace_id = session.scalar(select(ProjectWorkspace.id))
        session.add(
            TaskProjection(
                task_id="TASK-1",
                project_workspace_id=workspace_id,
                status_message_id="MESSAGE-2",
                projection_hash="sha256:new-projection",
            )
        )
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(ExecutionBudget(task_id="TASK-2", model_reservations=31))
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(ExecutionBudget(task_id="TASK-2", consultation_reservations=16))
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(ExecutionBudget(task_id="TASK-2", plan_revision_reservations=4))
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(ExecutionBudget(task_id="TASK-2", implementation_revision_reservations=4))
