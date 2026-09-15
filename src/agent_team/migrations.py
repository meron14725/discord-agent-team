"""Explicit, additive database schema migrations."""

from sqlalchemy import CheckConstraint, MetaData, inspect, text
from sqlalchemy.schema import CreateTable

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


def _apply_v3(connection, metadata):
    """Raise execution ceilings without resetting usage or approval records."""
    for name in ("consultations", "execution_budgets"):
        table = metadata.tables[name]
        if connection.dialect.name == "sqlite":
            # These leaf tables have no incoming foreign keys. Copy every column,
            # including identity and usage, before replacing the old constraints.
            copied = MetaData()
            for source in metadata.sorted_tables:
                source.to_metadata(copied)
            replacement = copied.tables[name]
            replacement.name = name + "_v3"
            connection.execute(CreateTable(replacement))
            columns = ", ".join('"' + column.name + '"' for column in table.columns)
            connection.execute(text(
                f'INSERT INTO "{name}_v3" ({columns}) SELECT {columns} FROM "{name}"'
            ))
            connection.execute(text(f'DROP TABLE "{name}"'))
            connection.execute(text(f'ALTER TABLE "{name}_v3" RENAME TO "{name}"'))
            for index in table.indexes:
                index.create(connection)
        elif connection.dialect.name == "postgresql":
            for constraint in table.constraints:
                if isinstance(constraint, CheckConstraint):
                    connection.execute(text(
                        f'ALTER TABLE "{name}" DROP CONSTRAINT "{constraint.name}", '
                        f'ADD CONSTRAINT "{constraint.name}" CHECK ({constraint.sqltext})'
                    ))
        else:
            raise RuntimeError("Budget migration requires SQLite or PostgreSQL")


def migrate(engine, metadata):
    """Bring the database to v3 in one transaction per invocation."""
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
        if 3 not in versions:
            _apply_v3(connection, metadata)
            _record_version(connection, 3)
