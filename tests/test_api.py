from datetime import date, datetime, timezone
import pytest
from httpx import ASGITransport, AsyncClient

from vrmmeter.db.models import HourlyConsumption, Installation, MeterReading
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

        # Favicon
        res_f1 = await client.get("/favicon.svg")
        assert res_f1.status_code == 200
        assert "image/svg+xml" in res_f1.headers["content-type"]
        assert "<svg" in res_f1.text

        res_f2 = await client.get("/favicon.ico")
        assert res_f2.status_code == 200
        assert "<svg" in res_f2.text

        # Self-hosted static assets
        res_css = await client.get("/static/css/tailwind.css")
        assert res_css.status_code == 200
        assert len(res_css.text) > 1000

        for static_file in ["alpine.min.js", "chart.umd.min.js"]:
            res_js = await client.get(f"/static/js/{static_file}")
            assert res_js.status_code == 200
            assert len(res_js.text) > 1000





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
            "currency": "GBP",
            "is_active": True,
        }
        res = await client.post("/api/installations", json=payload)
        assert res.status_code == 201
        data = res.json()
        inst_id = data["id"]
        assert data["name"] == "API Test House"
        assert data["nominal_voltage_v"] == 230.0
        assert data["currency"] == "GBP"

        # List
        res_list = await client.get("/api/installations")
        assert res_list.status_code == 200
        items = res_list.json()
        assert len(items) >= 1

        # Update currency
        res_up = await client.put(
            f"/api/installations/{inst_id}",
            json={"name": "API Test House (Updated)", "nominal_voltage_v": 220.0, "currency": "PLN"},
        )
        assert res_up.status_code == 200
        assert res_up.json()["name"] == "API Test House (Updated)"
        assert res_up.json()["nominal_voltage_v"] == 220.0
        assert res_up.json()["currency"] == "PLN"

        # Create price without explicit currency -> should inherit installation currency (PLN)
        res_p_auto = await client.post(
            "/api/prices",
            json={
                "installation_id": inst_id,
                "effective_from": "2026-09-01",
                "day_price_per_kwh": 0.85,
                "night_price_per_kwh": 0.45,
            },
        )
        assert res_p_auto.status_code == 201
        assert res_p_auto.json()["currency"] == "PLN"

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


@pytest.mark.asyncio
async def test_api_discover_without_token_fails(override_db, db_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Without token, discover should return 400
        res = await client.post("/api/installations/discover")
        assert res.status_code == 400
        assert "No VRM API token configured" in res.json()["detail"]


@pytest.mark.asyncio
async def test_api_load_demo_installations(override_db, db_session):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/installations/load-demo")
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["imported_count"] == 2
        site_ids = [inst["site_id"] for inst in data["installations"]]
        assert "1001" in site_ids
        assert "1002" in site_ids


@pytest.mark.asyncio
async def test_api_readings_crud_and_deltas(
    override_db, db_session, sample_installation: Installation
):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Initial reading exists (10000, 5000). Check API list.
        res = await client.get(f"/api/readings?installation_id={sample_installation.id}")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 1
        assert isinstance(data[0]["day_reading_kwh"], int)
        assert isinstance(data[0]["night_reading_kwh"], int)
        assert data[0]["day_reading_kwh"] == 10000
        assert data[0]["night_reading_kwh"] == 5000
        assert data[0]["delta_day_str"] == "(0)"
        assert data[0]["delta_night_str"] == "(0)"
        assert data[0]["delta_total_str"] == "(0)"

        # 2. Add hourly consumption for Jan 15
        h = HourlyConsumption(
            installation_id=sample_installation.id,
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            raw_grid_import_kwh=900.0,
            grid_voltage_v=220.0,
            is_voltage_fallback=False,
            loss_coefficient=1.0,
            corrected_grid_import_kwh=900.0,
            tariff_zone="DAY",
            day_kwh=900.0,
            night_kwh=0.0,
        )
        db_session.add(h)
        await db_session.commit()

        # 3. Create second reading via POST /api/readings for Feb 1
        # Expected day: 10000 + 900 = 10900. Entered: 11100 (+200).
        reading_payload = {
            "installation_id": sample_installation.id,
            "timestamp": "2026-02-01T00:00:00Z",
            "day_reading_kwh": 11100,
            "night_reading_kwh": 5000,
            "notes": "Feb reading via API",
        }
        res_post = await client.post("/api/readings", json=reading_payload)
        assert res_post.status_code == 201
        post_data = res_post.json()
        assert isinstance(post_data["day_reading_kwh"], int)
        assert post_data["day_reading_kwh"] == 11100

        # 4. Check GET /api/readings
        res_list = await client.get(f"/api/readings?installation_id={sample_installation.id}")
        assert res_list.status_code == 200
        items = res_list.json()
        assert len(items) == 2
        # Order is newest first
        assert isinstance(items[0]["day_reading_kwh"], int)
        assert items[0]["day_reading_kwh"] == 11100
        assert items[0]["delta_day_kwh"] == 200.0
        assert items[0]["delta_day_str"] == "(+200)"
        assert items[0]["delta_total_str"] == "(+200)"
        assert items[1]["delta_day_str"] == "(0)"

        # 5. Check HTML view rendering
        res_html = await client.get(f"/readings?installation_id={sample_installation.id}")
        assert res_html.status_code == 200
        assert "(+200)" in res_html.text
        assert "(0)" in res_html.text
        assert "11100.0" not in res_html.text
        assert "10000.0" not in res_html.text
        # Verify uniform grey delta styling and right-aligned layout
        assert '<span class="w-16 text-right text-xs font-normal text-slate-400">(+200)</span>' in res_html.text
        assert '<span class="w-16 text-right text-xs font-normal text-slate-400">(0)</span>' in res_html.text

        # 6. Update loss coefficients in settings via PUT /api/installations/{id}
        # Change loss_base_factor to 0.10 (10% loss).
        # recalculate_installation_losses will update h.corrected_grid_import_kwh and h.day_kwh
        # from 900.0 to (900 + 0.015) * 1.10 = 990.0165 (approx 990.02)
        res_up = await client.put(
            f"/api/installations/{sample_installation.id}",
            json={"loss_base_factor": 0.10, "avr_idle_loss_kwh": 0.0},
        )
        assert res_up.status_code == 200

        # Check that GET /api/readings immediately reflects the new delta!
        # Expected day was 10000 + 900 * 1.10 = 10990.
        # Entered = 11100 -> delta = 11100 - 10990 = +110!
        res_list_after = await client.get(f"/api/readings?installation_id={sample_installation.id}")
        assert res_list_after.status_code == 200
        items_after = res_list_after.json()
        assert items_after[0]["delta_day_kwh"] == 110.0
        assert items_after[0]["delta_day_str"] == "(+110)"


