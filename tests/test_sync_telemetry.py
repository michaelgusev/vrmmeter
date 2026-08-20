from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import HourlyConsumption, Installation, TelemetryParameter
from vrmmeter.services.sync_service import sync_installation
from vrmmeter.services.telemetry_service import ingest_telemetry_batch, parse_and_ingest_csv
from vrmmeter.services.vrm_client import MockVrmClient


@pytest.mark.asyncio
async def test_sync_installation_and_telemetry(
    db_session: AsyncSession, sample_installation: Installation
):
    mock_client = MockVrmClient()
    start_dt = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 3, 1, 23, 0, 0, tzinfo=timezone.utc)

    # 1. Sync from mock VRM
    records_count = await sync_installation(
        session=db_session,
        installation=sample_installation,
        start_dt=start_dt,
        end_dt=end_dt,
        vrm_client=mock_client,
    )
    assert records_count == 24

    # Check records in DB
    stmt = (
        select(HourlyConsumption)
        .where(HourlyConsumption.installation_id == sample_installation.id)
        .order_by(HourlyConsumption.timestamp.asc())
    )
    res = await db_session.execute(stmt)
    records = res.scalars().all()
    assert len(records) == 24

    # Initial records used fallback nominal voltage (220.0V)
    assert records[0].is_voltage_fallback is True
    assert records[0].grid_voltage_v == 220.0
    initial_corrected = records[0].corrected_grid_import_kwh

    # 2. Ingest external grid voltage telemetry for hour 0 (e.g. 242.0V)
    await ingest_telemetry_batch(
        session=db_session,
        installation_id=sample_installation.id,
        readings=[
            {
                "timestamp": start_dt,
                "metric_name": "grid_voltage_v",
                "metric_value": 242.0,
            }
        ],
    )

    # 3. Verify record 0 was updated with new voltage and recalculated loss
    stmt_updated = select(HourlyConsumption).where(
        HourlyConsumption.installation_id == sample_installation.id,
        HourlyConsumption.timestamp == start_dt,
    )
    res_updated = await db_session.execute(stmt_updated)
    updated_rec = res_updated.scalar_one_or_none()

    assert updated_rec.grid_voltage_v == 242.0
    assert updated_rec.is_voltage_fallback is False
    assert updated_rec.loss_coefficient > 1.03  # 1.035 with voltage penalty
    assert updated_rec.corrected_grid_import_kwh > initial_corrected


@pytest.mark.asyncio
async def test_csv_telemetry_import(
    db_session: AsyncSession, sample_installation: Installation
):
    csv_data = """timestamp,grid_voltage_v
2026-03-02 00:00:00,215.5
2026-03-02 01:00:00,218.0
2026-03-02 02:00:00,224.2
"""
    count = await parse_and_ingest_csv(
        session=db_session,
        installation_id=sample_installation.id,
        csv_content=csv_data,
    )
    assert count == 3

    stmt = select(TelemetryParameter).where(
        TelemetryParameter.installation_id == sample_installation.id
    )
    res = await db_session.execute(stmt)
    items = res.scalars().all()
    assert len(items) == 3


def test_parse_vrm_gc_gb_matrix_stats():
    from vrmmeter.services.vrm_client import VictronVrmClient

    client = VictronVrmClient(token="dummy")
    start_dt = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 3, 1, 2, 0, 0, tzinfo=timezone.utc)

    # Standard Victron matrix response with Gc, Gb, Tg
    raw_payload = {
        "records": {
            "Gc": [
                [1772323200000, 1.2],
                [1772326800000, 0.8],
            ],
            "Gb": [
                [1772323200000, 0.3],
                [1772326800000, 0.1],
            ],
            "Tg": [
                [1772323200000, 0.0],
                [1772326800000, 0.0],
            ],
        }
    }

    records = client._parse_vrm_stats_response(raw_payload["records"], start_dt, end_dt)
    assert len(records) == 2
    # Hour 1: Gc (1.2) + Gb (0.3) = 1.5
    assert records[0].grid_import_kwh == pytest.approx(1.5, abs=0.001)
    # Hour 2: Gc (0.8) + Gb (0.1) = 0.9
    assert records[1].grid_import_kwh == pytest.approx(0.9, abs=0.001)


@pytest.mark.asyncio
async def test_victron_vrm_client_get_installation_stats_mocked(monkeypatch):
    from vrmmeter.services.vrm_client import VictronVrmClient
    import httpx

    client = VictronVrmClient(token="test_token")
    start_dt = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 3, 30, 0, 0, 0, tzinfo=timezone.utc)

    # Mock httpx.AsyncClient.get
    async def mock_get(self, url, headers=None, params=None):
        class MockResponse:
            status_code = 200
            def json(self):
                return {
                    "records": {
                        "Gc": [[1772323200000, 2.5]],
                        "Tg": [[1772323200000, 0.0]],
                    }
                }
            def raise_for_status(self):
                pass
        return MockResponse()

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    records = await client.get_installation_stats("site_1001", start_dt, end_dt)
    assert len(records) >= 1
    assert records[0].grid_import_kwh == 2.5


@pytest.mark.asyncio
async def test_demo_site_sync_uses_mock_and_skips_live_vrm(
    db_session: AsyncSession, sample_installation: Installation
):
    from vrmmeter.services.vrm_client import VictronVrmClient

    # Mark sample installation as demo
    sample_installation.is_demo = True
    await db_session.commit()

    # Pass live client, which should be ignored for demo installations
    live_client = VictronVrmClient(token="real_live_token")

    start_dt = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(2026, 3, 1, 5, 0, 0, tzinfo=timezone.utc)

    # Sync should succeed using MockVrmClient without raising an error or calling external API
    count = await sync_installation(
        session=db_session,
        installation=sample_installation,
        start_dt=start_dt,
        end_dt=end_dt,
        vrm_client=live_client,
    )
    assert count == 6
