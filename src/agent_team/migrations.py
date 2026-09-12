"""Explicit, additive database schema migrations."""

from sqlalchemy import inspect, text

V1_TABLES = (
    "tasks",
    "spec_versions",
    "approvals",
    "reviews",
    "agent_runs",
    "artifacts",
    "external_operations",
    "events",
    "outbox",
    "jobs",
    "discord_changes",
)

V2_TABLES = (
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
)

TASK_V2_COLUMNS = {
    "workflow_version": "INTEGER NOT NULL DEFAULT 1",
    "requirements_reference_id": "VARCHAR",
    "current_plan_version_id": "VARCHAR",
}


def _create_tables(connection, metadata, names):
    metadata.create_all(connection, tables=[metadata.tables[name] for name in names], checkfirst=True)


def _record_version(connection, version):
    connection.execute(
        text("INSERT INTO schema_migrations (version) VALUES (:version) ON CONFLICT DO NOTHING"),
        {"version": version},
    )


def _apply_v2(connection, metadata):
    columns = {column["name"] for column in inspect(connection).get_columns("tasks")}
    for name, ddl in TASK_V2_COLUMNS.items():
        if name not in columns:
            connection.execute(text(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}"))
    _create_tables(connection, metadata, V2_TABLES)


def migrate(engine, metadata):
    """Bring a new or v1 database to v2 in one transaction per invocation."""
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
        )
        _create_tables(connection, metadata, V1_TABLES)
        _record_version(connection, 1)
        versions = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
        if 2 not in versions:
            _apply_v2(connection, metadata)
            _record_version(connection, 2)
