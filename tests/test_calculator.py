from datetime import date, datetime, timedelta, timezone
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import HourlyConsumption, Installation, MeterReading
from vrmmeter.services.calculator import (
    calculate_monthly_report,
    estimate_meter_reading,
    format_delta,
    get_effective_price,
    get_readings_with_deltas,
)


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


@pytest.mark.asyncio
async def test_august_21_midnight_crossover_timezone_alignment(
    db_session: AsyncSession, sample_installation: Installation
):
    """
    At 2026-08-20 22:00:00 UTC, in Europe/Kyiv (UTC+3) it is 2026-08-21 01:00:00.
    The report for August 2026 must display both August 20 and August 21 columns.
    """
    # Aug 20 18:00 UTC = Aug 20 21:00 Kyiv
    rec_aug20 = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 8, 20, 18, 0, 0, tzinfo=timezone.utc),
        raw_grid_import_kwh=2.0,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.03,
        corrected_grid_import_kwh=2.06,
        tariff_zone="DAY",
        day_kwh=2.06,
        night_kwh=0.0,
    )
    # Aug 20 22:00 UTC = Aug 21 01:00 Kyiv
    rec_aug21 = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 8, 20, 22, 0, 0, tzinfo=timezone.utc),
        raw_grid_import_kwh=1.0,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.03,
        corrected_grid_import_kwh=1.03,
        tariff_zone="NIGHT",
        day_kwh=0.0,
        night_kwh=1.03,
    )
    db_session.add(rec_aug20)
    db_session.add(rec_aug21)
    await db_session.commit()

    report = await calculate_monthly_report(
        session=db_session,
        installation=sample_installation,
        year=2026,
        month=8,
    )

    dates = [item.date for item in report.daily_breakdown]
    assert date(2026, 8, 20) in dates
    assert date(2026, 8, 21) in dates


def test_format_delta():
    assert format_delta(0.0) == "(0)"
    assert format_delta(None) == "(0)"
    assert format_delta(0.0001) == "(0)"
    assert format_delta(-0.0001) == "(0)"
    assert format_delta(200.0) == "(+200)"
    assert format_delta(-50.0) == "(-50)"
    assert format_delta(200.5) == "(+200.5)"
    assert format_delta(-50.25) == "(-50.25)"


@pytest.mark.asyncio
async def test_get_readings_with_deltas_single_and_multiple(
    db_session: AsyncSession, sample_installation: Installation
):
    # sample_installation already has 1 reference reading at 2026-01-01 (10000.0, 5000.0)
    readings = await get_readings_with_deltas(db_session, sample_installation.id)
    assert len(readings) == 1
    assert readings[0].delta_day_kwh == 0.0
    assert readings[0].delta_night_kwh == 0.0
    assert readings[0].delta_total_kwh == 0.0
    assert readings[0].delta_day_str == "(0)"
    assert readings[0].delta_night_str == "(0)"
    assert readings[0].delta_total_str == "(0)"
    assert readings[0].expected_day_reading == 10000.0
    assert readings[0].expected_night_reading == 5000.0
    assert readings[0].expected_total_reading == 15000.0

    # Add VRM consumption between 2026-01-01 and 2026-02-01:
    # Day VRM = 900.0, Night VRM = 450.0
    dt_day = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
    dt_night = datetime(2026, 1, 10, 2, 0, 0, tzinfo=timezone.utc)
    h1 = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=dt_day,
        raw_grid_import_kwh=900.0,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.0,
        corrected_grid_import_kwh=900.0,
        tariff_zone="DAY",
        day_kwh=900.0,
        night_kwh=0.0,
    )
    h2 = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=dt_night,
        raw_grid_import_kwh=450.0,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.0,
        corrected_grid_import_kwh=450.0,
        tariff_zone="NIGHT",
        day_kwh=0.0,
        night_kwh=450.0,
    )
    db_session.add(h1)
    db_session.add(h2)

    # Add second reference reading at 2026-02-01:
    # Expected: day = 10000 + 900 = 10900, night = 5000 + 450 = 5450, total = 16350
    # Entered: day = 11100 (+200), night = 5500 (+50), total = 16600 (+250)
    ref2 = MeterReading(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc),
        day_reading_kwh=11100.0,
        night_reading_kwh=5500.0,
        is_reference=True,
        notes="Feb reading",
    )
    db_session.add(ref2)
    await db_session.commit()

    # Query readings with deltas
    readings = await get_readings_with_deltas(db_session, sample_installation.id)
    assert len(readings) == 2
    # Result is descending by timestamp: readings[0] is Feb 1, readings[1] is Jan 1
    r_feb = readings[0]
    r_jan = readings[1]

    assert r_jan.delta_day_str == "(0)"
    assert r_jan.delta_night_str == "(0)"
    assert r_jan.delta_total_str == "(0)"

    assert r_feb.expected_day_reading == 10900.0
    assert r_feb.expected_night_reading == 5450.0
    assert r_feb.expected_total_reading == 16350.0
    assert r_feb.delta_day_kwh == 200.0
    assert r_feb.delta_night_kwh == 50.0
    assert r_feb.delta_total_kwh == 250.0
    assert r_feb.delta_day_str == "(+200)"
    assert r_feb.delta_night_str == "(+50)"
    assert r_feb.delta_total_str == "(+250)"

    # Add VRM consumption between Feb 1 and Mar 1:
    # Day VRM = 920.0, Night VRM = 410.0
    h3 = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 2, 10, 12, 0, 0, tzinfo=timezone.utc),
        raw_grid_import_kwh=920.0,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.0,
        corrected_grid_import_kwh=920.0,
        tariff_zone="DAY",
        day_kwh=920.0,
        night_kwh=0.0,
    )
    h4 = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 2, 10, 2, 0, 0, tzinfo=timezone.utc),
        raw_grid_import_kwh=410.0,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.0,
        corrected_grid_import_kwh=410.0,
        tariff_zone="NIGHT",
        day_kwh=0.0,
        night_kwh=410.0,
    )
    db_session.add(h3)
    db_session.add(h4)

    # Add third reference reading at 2026-03-01:
    # Expected from Feb 1: day = 11100 + 920 = 12020, night = 5500 + 410 = 5910, total = 17930
    # Entered: day = 12000 (-20), night = 5900 (-10), total = 17900 (-30)
    ref3 = MeterReading(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
        day_reading_kwh=12000.0,
        night_reading_kwh=5900.0,
        is_reference=True,
        notes="Mar reading",
    )
    db_session.add(ref3)
    await db_session.commit()

    readings = await get_readings_with_deltas(db_session, sample_installation.id)
    assert len(readings) == 3
    r_mar = readings[0]
    assert r_mar.expected_day_reading == 12020.0
    assert r_mar.expected_night_reading == 5910.0
    assert r_mar.expected_total_reading == 17930.0
    assert r_mar.delta_day_kwh == -20.0
    assert r_mar.delta_night_kwh == -10.0
    assert r_mar.delta_total_kwh == -30.0
    assert r_mar.delta_day_str == "(-20)"
    assert r_mar.delta_night_str == "(-10)"
    assert r_mar.delta_total_str == "(-30)"


@pytest.mark.asyncio
async def test_get_readings_with_deltas_recalculation_on_delete(
    db_session: AsyncSession, sample_installation: Installation
):
    # Sample has Jan 1 (10000, 5000).
    # Add VRM data for Jan (day 500) and Feb (day 300)
    h1 = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        raw_grid_import_kwh=500.0,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.0,
        corrected_grid_import_kwh=500.0,
        tariff_zone="DAY",
        day_kwh=500.0,
        night_kwh=0.0,
    )
    h2 = HourlyConsumption(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc),
        raw_grid_import_kwh=300.0,
        grid_voltage_v=220.0,
        is_voltage_fallback=False,
        loss_coefficient=1.0,
        corrected_grid_import_kwh=300.0,
        tariff_zone="DAY",
        day_kwh=300.0,
        night_kwh=0.0,
    )
    ref2 = MeterReading(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc),
        day_reading_kwh=10550.0,  # exp: 10500, delta: +50
        night_reading_kwh=5000.0,
        is_reference=True,
    )
    ref3 = MeterReading(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc),
        day_reading_kwh=10850.0,  # exp from ref2: 10550 + 300 = 10850, delta: 0
        night_reading_kwh=5000.0,
        is_reference=True,
    )
    db_session.add_all([h1, h2, ref2, ref3])
    await db_session.commit()

    readings = await get_readings_with_deltas(db_session, sample_installation.id)
    assert len(readings) == 3
    assert readings[0].delta_day_str == "(0)"  # Mar 1: 10850 - 10850 = 0
    assert readings[1].delta_day_str == "(+50)"  # Feb 1: 10550 - 10500 = +50

    # Delete ref2 (Feb 1 reading)
    await db_session.delete(ref2)
    await db_session.commit()

    # Now Mar 1 is calculated directly against Jan 1!
    # Expected day from Jan 1: 10000 + 500 + 300 = 10800.
    # Entered: 10850 -> delta = +50!
    readings = await get_readings_with_deltas(db_session, sample_installation.id)
    assert len(readings) == 2
    assert readings[0].expected_day_reading == 10800.0
    assert readings[0].delta_day_kwh == 50.0
    assert readings[0].delta_day_str == "(+50)"

