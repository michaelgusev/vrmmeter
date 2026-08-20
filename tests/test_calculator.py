from datetime import date, datetime, timedelta, timezone
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import HourlyConsumption, Installation, MeterReading
from vrmmeter.services.calculator import calculate_monthly_report, estimate_meter_reading, get_effective_price


@pytest.mark.asyncio
async def test_get_effective_price(db_session: AsyncSession, sample_installation: Installation):
    # Sample installation has prices effective 2026-01-01 (0.30/0.15) and 2026-06-01 (0.35/0.18)
    p_may = await get_effective_price(db_session, sample_installation.id, date(2026, 5, 15))
    assert p_may is not None
    assert p_may.day_price_per_kwh == 0.30
    assert p_may.night_price_per_kwh == 0.15

    p_july = await get_effective_price(db_session, sample_installation.id, date(2026, 7, 1))
    assert p_july is not None
    assert p_july.day_price_per_kwh == 0.35
    assert p_july.night_price_per_kwh == 0.18

    p_past = await get_effective_price(db_session, sample_installation.id, date(2025, 12, 1))
    assert p_past is None


@pytest.mark.asyncio
async def test_estimate_meter_reading_single_reference(
    db_session: AsyncSession, sample_installation: Installation
):
    # Initial reference reading in sample_installation is (10000.0, 5000.0) at 2026-01-01 00:00:00 UTC
    # Add 5 hours of DAY consumption (2.0 kWh each) and 3 hours of NIGHT consumption (1.0 kWh each)
    base_dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(8):
        dt = base_dt + timedelta(hours=i)
        is_night = i < 3
        h = HourlyConsumption(
            installation_id=sample_installation.id,
            timestamp=dt,
            raw_grid_import_kwh=1.0 if is_night else 2.0,
            raw_grid_export_kwh=0.0,
            grid_voltage_v=220.0,
            is_voltage_fallback=False,
            loss_coefficient=1.03,
            corrected_grid_import_kwh=1.0 if is_night else 2.0,
            tariff_zone="NIGHT" if is_night else "DAY",
            day_kwh=0.0 if is_night else 2.0,
            night_kwh=1.0 if is_night else 0.0,
        )
        db_session.add(h)
    await db_session.commit()

    target_time = base_dt + timedelta(hours=8)
    res = await estimate_meter_reading(db_session, sample_installation.id, target_time)

    assert res.delta_day_kwh == 10.0  # 5 hours * 2.0
    assert res.delta_night_kwh == 3.0  # 3 hours * 1.0
    assert res.estimated_day_reading == 10010.0  # 10000 + 10
    assert res.estimated_night_reading == 5003.0  # 5000 + 3
    assert res.is_extrapolated_from_reference is True


@pytest.mark.asyncio
async def test_estimate_meter_reading_multiple_references(
    db_session: AsyncSession, sample_installation: Installation
):
    # Add second reference reading at 2026-01-10 with calibrated values
    ref2_dt = datetime(2026, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
    ref2 = MeterReading(
        installation_id=sample_installation.id,
        timestamp=ref2_dt,
        day_reading_kwh=10250.0,
        night_reading_kwh=5120.0,
        is_reference=True,
        notes="Mid-month check",
    )
    db_session.add(ref2)

    # Add consumption after ref2
    for i in range(5):
        dt = ref2_dt + timedelta(hours=i)
        h = HourlyConsumption(
            installation_id=sample_installation.id,
            timestamp=dt,
            raw_grid_import_kwh=3.0,
            grid_voltage_v=220.0,
            is_voltage_fallback=False,
            loss_coefficient=1.03,
            corrected_grid_import_kwh=3.0,
            tariff_zone="DAY",
            day_kwh=3.0,
            night_kwh=0.0,
        )
        db_session.add(h)
    await db_session.commit()

    target_time = ref2_dt + timedelta(hours=5)
    res = await estimate_meter_reading(db_session, sample_installation.id, target_time)

    # Must be anchored to ref2 (10250.0 + 15.0 = 10265.0)
    assert res.reference_reading is not None
    assert res.reference_reading.day_reading_kwh == 10250.0
    assert res.delta_day_kwh == 15.0
    assert res.estimated_day_reading == 10265.0
    assert res.estimated_night_reading == 5120.0


@pytest.mark.asyncio
async def test_calculate_monthly_report(
    db_session: AsyncSession, sample_installation: Installation
):
    # Ingest synthetic consumption for January 2026 aligned to Kyiv timezone
    import zoneinfo
    tz = zoneinfo.ZoneInfo(sample_installation.timezone)
    start_local = datetime(2026, 1, 1, 0, 0, 0, tzinfo=tz)
    for d in range(5):  # 5 full local days
        for h in range(24):
            dt_local = start_local + timedelta(days=d, hours=h)
            dt_utc = dt_local.astimezone(timezone.utc)
            is_night = h < 7 or h >= 23
            kwh = 1.0 if is_night else 2.0
            rec = HourlyConsumption(
                installation_id=sample_installation.id,
                timestamp=dt_utc,
                raw_grid_import_kwh=kwh,
                grid_voltage_v=220.0,
                is_voltage_fallback=False,
                loss_coefficient=1.03,
                corrected_grid_import_kwh=kwh * 1.03,
                tariff_zone="NIGHT" if is_night else "DAY",
                day_kwh=0.0 if is_night else kwh * 1.03,
                night_kwh=kwh * 1.03 if is_night else 0.0,
            )
            db_session.add(rec)
    await db_session.commit()

    report = await calculate_monthly_report(
        session=db_session,
        installation=sample_installation,
        year=2026,
        month=1,
    )

    assert report.installation_id == sample_installation.id
    assert report.total_kwh > 0
    assert report.day_kwh > 0
    assert report.night_kwh > 0
    assert report.day_rate == 0.30
    assert report.night_rate == 0.15
    assert report.total_bill > 0
    assert len(report.daily_breakdown) == 5


@pytest.mark.asyncio
async def test_august_month_boundary_timezone_alignment(
    db_session: AsyncSession, sample_installation: Installation
):
    """
    In Europe/Kyiv (UTC+3 in summer), 2026-08-01 00:00:00 local is 2026-07-31 21:00:00 UTC.
    The report for August 2026 MUST group this record under 2026-08-01 and NOT 2026-07-31.
    """
    # 2026-07-31 21:00:00 UTC = 2026-08-01 00:00:00 Kyiv time
    dt_utc = datetime(2026, 7, 31, 21, 0, 0, tzinfo=timezone.utc)
    rec = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=dt_utc,
        raw_grid_import_kwh=1.5,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.03,
        corrected_grid_import_kwh=1.545,
        tariff_zone="NIGHT",
        day_kwh=0.0,
        night_kwh=1.545,
    )
    db_session.add(rec)
    await db_session.commit()

    report = await calculate_monthly_report(
        session=db_session,
        installation=sample_installation,
        year=2026,
        month=8,
    )

    assert len(report.daily_breakdown) == 1
    # MUST be 2026-08-01 in installation local timezone!
    assert report.daily_breakdown[0].date == date(2026, 8, 1)
    assert date(2026, 7, 31) not in [item.date for item in report.daily_breakdown]
