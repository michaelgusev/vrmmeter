from datetime import date, datetime, timezone
import pytest
from httpx import ASGITransport, AsyncClient

from vrmmeter.db.models import Installation
from vrmmeter.db.session import get_db
from vrmmeter.main import app


@pytest.fixture
def override_db(db_session):
    async def _get_db():
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_html_views_render(override_db, db_session, sample_installation: Installation):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Dashboard
        res = await client.get("/")
        assert res.status_code == 200
        assert "VRM" in res.text

        # Meter Tool
        res_m = await client.get(f"/meter-tool?installation_id={sample_installation.id}")
        assert res_m.status_code == 200
        assert "Meter Calculator" in res_m.text

        # Readings
        res_r = await client.get(f"/readings?installation_id={sample_installation.id}")
        assert res_r.status_code == 200
        assert "Reference Meter Readings" in res_r.text

        # Tariffs
        res_t = await client.get(f"/tariffs?installation_id={sample_installation.id}")
        assert res_t.status_code == 200
        assert "Tariffs" in res_t.text

        # Telemetry
        res_tel = await client.get(f"/telemetry?installation_id={sample_installation.id}")
        assert res_tel.status_code == 200
        assert "Grid Voltage" in res_tel.text

        # Settings
        res_s = await client.get("/settings")
        assert res_s.status_code == 200
        assert "VRM" in res_s.text


@pytest.mark.asyncio
async def test_api_installations_crud(override_db, db_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Create
        payload = {
            "name": "API Test House",
            "vrm_site_id": "9999",
            "timezone": "Europe/Warsaw",
            "nominal_voltage_v": 230.0,
            "avr_idle_loss_kwh": 0.02,
            "loss_base_factor": 0.04,
            "loss_voltage_factor": 0.05,
            "tariff_schedule": [
                {"zone": "NIGHT", "start": "22:00", "end": "06:00"},
                {"zone": "DAY", "start": "06:00", "end": "22:00"},
            ],
            "is_active": True,
        }
        res = await client.post("/api/installations", json=payload)
        assert res.status_code == 201
        data = res.json()
        inst_id = data["id"]
        assert data["name"] == "API Test House"
        assert data["nominal_voltage_v"] == 230.0

        # List
        res_list = await client.get("/api/installations")
        assert res_list.status_code == 200
        items = res_list.json()
        assert len(items) >= 1

        # Update
        res_up = await client.put(
            f"/api/installations/{inst_id}",
            json={"name": "API Test House (Updated)", "nominal_voltage_v": 220.0},
        )
        assert res_up.status_code == 200
        assert res_up.json()["name"] == "API Test House (Updated)"
        assert res_up.json()["nominal_voltage_v"] == 220.0

        # Delete
        res_del = await client.delete(f"/api/installations/{inst_id}")
        assert res_del.status_code == 204


@pytest.mark.asyncio
async def test_api_pricing_and_reports(override_db, db_session, sample_installation: Installation):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Add price
        price_payload = {
            "installation_id": sample_installation.id,
            "effective_from": "2026-08-01",
            "day_price_per_kwh": 0.40,
            "night_price_per_kwh": 0.20,
            "standing_charge_daily": 0.60,
            "currency": "EUR",
        }
        res_p = await client.post("/api/prices", json=price_payload)
        assert res_p.status_code == 201

        # Get monthly report
        res_rep = await client.get(
            f"/api/reports/monthly?installation_id={sample_installation.id}&year=2026&month=8"
        )
        assert res_rep.status_code == 200
        rep_data = res_rep.json()
        assert rep_data["year"] == 2026
        assert rep_data["month"] == 8
        assert rep_data["day_rate"] == 0.40
        assert rep_data["night_rate"] == 0.20


@pytest.mark.asyncio
async def test_api_meter_calculator(override_db, db_session, sample_installation: Installation):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        calc_payload = {
            "installation_id": sample_installation.id,
            "target_time": "2026-08-19T12:00:00Z",
        }
        res = await client.post("/api/calculate/meter", json=calc_payload)
        assert res.status_code == 200
        data = res.json()
        assert "estimated_day_reading" in data
        assert "estimated_night_reading" in data
        assert data["is_extrapolated_from_reference"] is True
