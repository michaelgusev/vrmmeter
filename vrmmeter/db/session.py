from collections.abc import AsyncGenerator
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vrmmeter.config import settings
from vrmmeter.db.models import Base

# Enable WAL mode and foreign keys for SQLite connections
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


engine: AsyncEngine = create_async_engine(
    settings.db_url,
    echo=settings.debug,
    future=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database tables and run lightweight migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def check_and_migrate(sync_conn):
            try:
                cursor = sync_conn.connection.cursor()
                cursor.execute("PRAGMA table_info(installations)")
                columns = [row[1] for row in cursor.fetchall()]
                if columns and "is_demo" not in columns:
                    cursor.execute("ALTER TABLE installations ADD COLUMN is_demo BOOLEAN DEFAULT 0 NOT NULL")
                if columns and "currency" not in columns:
                    cursor.execute("ALTER TABLE installations ADD COLUMN currency VARCHAR(8) DEFAULT 'EUR' NOT NULL")
                cursor.execute("UPDATE installations SET is_demo = 1 WHERE vrm_site_id IN ('1001', '1002')")
                cursor.close()
            except Exception:
                pass

        await conn.run_sync(check_and_migrate)
