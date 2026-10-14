import asyncio
from datetime import date, datetime, timezone
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from vrmmeter.db.models import Base, ElectricityPrice, Installation, MeterReading
from vrmmeter.services.vrm_client import MockVrmClient


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncSession:
    session_factory = async_sessionmaker(
        db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
async def sample_installation(db_session: AsyncSession) -> Installation:
    inst = Installation(
        id="test-inst-01",
        name="Test Residence",
        vrm_site_id="1001",
        timezone="Europe/Kyiv",
        nominal_voltage_v=220.0,
        avr_idle_loss_kwh=0.015,
        loss_base_factor=0.03,
        loss_voltage_factor=0.05,
        tariff_schedule=[
            {"zone": "NIGHT", "start": "23:00", "end": "07:00"},
            {"zone": "DAY", "start": "07:00", "end": "23:00"},
        ],
        currency="EUR",
        is_active=True,
    )
    db_session.add(inst)

    # Add electricity prices
    p1 = ElectricityPrice(
        installation_id=inst.id,
        effective_from=date(2026, 1, 1),
        day_price_per_kwh=0.30,
        night_price_per_kwh=0.15,
        standing_charge_daily=0.50,
        currency="EUR",
    )
    p2 = ElectricityPrice(
        installation_id=inst.id,
        effective_from=date(2026, 6, 1),
        day_price_per_kwh=0.35,
        night_price_per_kwh=0.18,
        standing_charge_daily=0.55,
        currency="EUR",
    )
    db_session.add_all([p1, p2])

    # Add baseline reference meter reading at start of year
    r1 = MeterReading(
        installation_id=inst.id,
        timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        day_reading_kwh=10000.0,
        night_reading_kwh=5000.0,
        is_reference=True,
        notes="Initial Year Reading",
    )
    db_session.add(r1)

    await db_session.commit()
    await db_session.refresh(inst)
    return inst


@pytest.fixture
def mock_vrm_client() -> MockVrmClient:
    return MockVrmClient()
