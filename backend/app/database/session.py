from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)


if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        """Make declared ON DELETE rules effective for every SQLite connection."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    from app.database import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == "sqlite":
        migrations = {
            "sources": {
                "source_url": "TEXT",
                "title": "VARCHAR(500) NOT NULL DEFAULT ''",
                "author": "VARCHAR(255) NOT NULL DEFAULT ''",
                "import_status": "VARCHAR(32) NOT NULL DEFAULT 'completed'",
                "import_error": "TEXT",
            },
            "clips": {
                "sequence_number": "INTEGER NOT NULL DEFAULT 1",
                "title": "VARCHAR(180) NOT NULL DEFAULT ''",
                "description": "TEXT NOT NULL DEFAULT ''",
                "hashtags": "JSON NOT NULL DEFAULT '[]'",
                "processing_status": "VARCHAR(32) NOT NULL DEFAULT 'processing'",
                "preview_path": "TEXT",
            },
            "publications": {
                "publish_metadata": "JSON NOT NULL DEFAULT '{}'",
                "requested_for": "DATETIME",
                "next_attempt_at": "DATETIME",
                # SQLite only permits constant defaults in ALTER TABLE. New
                # databases receive server defaults from the ORM model; old
                # databases are backfilled immediately below.
                "created_at": "DATETIME",
                "updated_at": "DATETIME",
            },
        }
        with engine.begin() as connection:
            inspector = inspect(connection)
            for table, definitions in migrations.items():
                columns = {column["name"] for column in inspector.get_columns(table)}
                for name, definition in definitions.items():
                    if name not in columns:
                        connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                        )
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_sources_source_url "
                "ON sources(source_url) WHERE source_url IS NOT NULL"
            )
            connection.exec_driver_sql(
                "UPDATE sources SET title = original_name WHERE title = ''"
            )
            connection.exec_driver_sql(
                "UPDATE clips SET processing_status = CASE "
                "WHEN export_path IS NOT NULL AND export_path <> '' THEN 'completed' ELSE 'processing' END"
            )
            connection.exec_driver_sql(
                "UPDATE clips SET preview_path = export_path "
                "WHERE preview_path IS NULL AND export_path IS NOT NULL"
            )
            connection.exec_driver_sql(
                "UPDATE publications SET requested_for = scheduled_for WHERE requested_for IS NULL"
            )
            connection.exec_driver_sql(
                "UPDATE publications SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"
            )
            connection.exec_driver_sql(
                "UPDATE publications SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
            )
