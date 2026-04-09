"""
Test configuration and shared fixtures.

Sets required environment variables BEFORE any app modules are imported
so pydantic-settings can resolve all required fields without a real .env.
"""

import os

# ── Must be set before any app import ────────────────────────────────────────
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-anthropic-key")
os.environ.setdefault("APOLLO_API_KEY",    "test-apollo-key")
os.environ.setdefault("GITHUB_TOKEN",      "test-github-token")
os.environ.setdefault("HUNTER_API_KEY",    "test-hunter-key")
os.environ.setdefault("CIRCLE_API_KEY",    "test-circle-key")
# Use SQLite so tests never need a running PostgreSQL instance
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock

from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from db.database import Base
from db.models import Search


# ── Shared in-memory engine (one per test session) ────────────────────────────

_TEST_ENGINE = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    echo=False,
    poolclass=StaticPool,
    connect_args={"check_same_thread": False},
)
_TestSessionLocal = async_sessionmaker(
    bind=_TEST_ENGINE,
    class_=AsyncSession,
    expire_on_commit=False,
)


@pytest.fixture(autouse=True, scope="session")
async def _create_tables():
    """Create all ORM tables once for the test session."""
    async with _TEST_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _TEST_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def db_session():
    """Provide a rollback-isolated AsyncSession for each test."""
    async with _TestSessionLocal() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def client(db_session):
    """
    HTTP AsyncClient pointing at the FastAPI app with:
      - get_db overridden to use the test SQLite session
      - lifespan skipped (create_tables already ran above)
    """
    from main import app
    from db.database import get_db

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    # Patch create_tables so the lifespan doesn't try to connect to Postgres
    from unittest.mock import patch, AsyncMock as _AsyncMock
    with patch("main.create_tables", new=_AsyncMock()), \
         patch("main._init_agent_addresses"):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def mock_llm():
    """
    AsyncMock that behaves like a ChatAnthropic instance.
    .ainvoke() returns a mock message whose .content is '{}' by default.
    .with_config() returns the same mock (so chaining works).
    """
    mock = AsyncMock()
    mock.ainvoke = AsyncMock(return_value=MagicMock(content="{}"))
    mock.with_config = MagicMock(return_value=mock)
    return mock


@pytest.fixture
def sample_search_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
async def running_search(db_session, sample_search_id) -> Search:
    """A Search row already persisted with status='running'."""
    from datetime import datetime, timezone
    search = Search(
        id=uuid.UUID(sample_search_id),
        job_description="Looking for a senior Python engineer",
        status="running",
    )
    db_session.add(search)
    await db_session.commit()
    return search
