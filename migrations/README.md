# DB migration

`agent-team migrate` (`DATABASE_URL` required) or orchestrator startup applies baseline v1.
The canonical DDL is SQLAlchemy metadata in `src/agent_team/db.py`; `schema_migrations`
records applied versions. `create_all` only creates absent tables and does not modify
existing columns. Future schema changes require explicit, versioned migration code,
a backup, and compatibility tests; do not expect automatic ALTERs.

スキーマv3は実行・相談・修正回数のDB制約を5倍に拡張する。PostgreSQLは制約の置換、SQLiteは相談・予算テーブルの全行コピーで移行し、既存回数・識別子・承認は保持する。
