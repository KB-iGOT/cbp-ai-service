import contextlib
from typing import AsyncIterator
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class DatabaseSessionManager:
    """
    A Singleton-like class to manage Async SQLAlchemy database sessions.
    
    Attributes:
        _engine (AsyncEngine): Private. The SQLAlchemy async engine.
        _sessionmaker (async_sessionmaker): Private. The factory for creating sessions.
    """
    _instance = None
    _engine: AsyncEngine | None = None
    _sessionmaker: async_sessionmaker | None = None

    def __new__(cls):
        # Ensure only one instance exists (Singleton pattern)
        if cls._instance is None:
            cls._instance = super(DatabaseSessionManager, cls).__new__(cls)
        return cls._instance

    def init(self, host: str):
        """
        Public method to initialize the database connection.
        """
        # Idempotency check: If already initialized, do nothing
        if self._engine is not None:
            return

        self._engine = create_async_engine(
            host,
            pool_size = 20,
            max_overflow = 40,
            pool_timeout = 30,
            pool_recycle = 1800,
            echo = False,
        )
        
        self._sessionmaker = async_sessionmaker(
            bind=self._engine,
            autoflush=False,
            expire_on_commit=False,
            class_=AsyncSession
        )

    async def close(self):
        """
        Public method to close the database connection cleanly.
        """
        if self._engine is None:
            return
        await self._engine.dispose()
        self._engine = None
        self._sessionmaker = None

    @contextlib.asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        """
        Public context manager for direct connection access (e.g., for creating tables).
        """
        if self._engine is None:
            raise Exception("DatabaseSessionManager is not initialized")

        async with self._engine.begin() as connection:
            yield connection

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """
        Public context manager for database sessions (e.g., for CRUD operations).
        """
        if self._sessionmaker is None:
            raise Exception("DatabaseSessionManager is not initialized")

        session = self._sessionmaker()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

sessionmanager = DatabaseSessionManager()

# Dependency Injection helper for FastAPI
async def get_db_session():
    async with sessionmanager.session() as session:
        yield session