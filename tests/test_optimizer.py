from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import HourlyConsumption, Installation, MeterReading
from vrmmeter.services.calculator import get_readings_with_deltas
from vrmmeter.services.optimizer import (
    ObservationData,
    apply_optimal_coefficients,
    calculate_optimal_coefficients,
    extract_observations_for_installation,
    predict_observation,
    run_least_squares,
    solve_3x3,
)


def test_solve_3x3():
    # Regular invertible system
    # 2x + y + z = 4
    # x + 3y + 2z = 5
    # x = 6  -> y = 15, z = -23
    A = [[2.0, 1.0, 1.0], [1.0, 3.0, 2.0], [1.0, 0.0, 0.0]]
    b = [4.0, 5.0, 6.0]
    res = solve_3x3(A, b)
    assert res is not None
    assert round(res[0], 2) == 6.0
    assert round(res[1], 2) == 15.0
    assert round(res[2], 2) == -23.0

    # Singular matrix
    A_sing = [[1.0, 2.0, 3.0], [2.0, 4.0, 6.0], [0.0, 1.0, 1.0]]
    assert solve_3x3(A_sing, [1.0, 2.0, 3.0]) is None


def test_run_least_squares_synthetic_recovery():
    """Verify that optimizer recovers ground-truth coefficients from synthetic observations."""
    true_base = 0.04
    true_idle = 0.02
    true_volt = 0.06

    observations = []
    for i in range(6):
        raw = 80.0 + i * 25.0
        v_dev = 0.02 + (i % 3) * 0.025
        hours = 168.0  # 1 week
        s_volt = raw * v_dev
        s_v = hours * v_dev
        y = (raw + true_idle * hours) * (1.0 + true_base + true_volt * v_dev)

        obs = ObservationData(
            s_raw=raw,
            s_volt=s_volt,
            n_hours=hours,
            s_v=s_v,
            y_actual=y,
            label=f"Obs {i}",
        )
        observations.append(obs)

    # Start from default priors: 0.03, 0.015, 0.05
    k_base, k_idle, k_volt = run_least_squares(
        observations, init_k_base=0.03, init_k_idle=0.015, init_k_volt=0.05
    )

    assert abs(k_base - true_base) < 0.001
    assert abs(k_idle - true_idle) < 0.001
    assert abs(k_volt - true_volt) < 0.001


def test_run_least_squares_zero_voltage_variation():
    """Verify stability and boundedness when grid voltage never deviates from nominal."""
    observations = []
    for i in range(5):
        raw = 50.0 + i * 20.0
        hours = 100.0
        # No voltage deviation: v_dev = 0
        y = (raw + 0.02 * hours) * (1.0 + 0.05)
        obs = ObservationData(
            s_raw=raw,
            s_volt=0.0,
            n_hours=hours,
            s_v=0.0,
            y_actual=y,
            label=f"Obs {i}",
        )
        observations.append(obs)

    # Should not crash, and should keep k_volt within reasonable bounded limits
    k_base, k_idle, k_volt = run_least_squares(
        observations, init_k_base=0.03, init_k_idle=0.015, init_k_volt=0.05
    )
    assert 0.0 <= k_base <= 0.50
    assert 0.0 <= k_idle <= 0.50
    assert 0.0 <= k_volt <= 1.00


@pytest.mark.asyncio
async def test_calculate_optimal_coefficients_insufficient_readings(
    db_session: AsyncSession, sample_installation: Installation
):
    """If fewer than 4 readings exist, can_optimize must be False with explanatory message."""
    # sample_installation fixture starts with 1 reading (Jan 1)
    metrics = await calculate_optimal_coefficients(db_session, sample_installation)
    assert metrics.can_optimize is False
    assert "At least 4 reference readings" in metrics.message
    assert metrics.current_readings_count == 1
    assert metrics.min_readings_required == 4

    # Add a second reading (still only 2 readings, 1 interval)
    r2 = MeterReading(
        installation_id=sample_installation.id,
        timestamp=datetime(2026, 1, 10, 0, 0, 0, tzinfo=timezone.utc),
        day_reading_kwh=10200.0,
        night_reading_kwh=5100.0,
        is_reference=True,
    )
    db_session.add(r2)
    await db_session.commit()

    metrics2 = await calculate_optimal_coefficients(db_session, sample_installation)
    assert metrics2.can_optimize is False
    assert metrics2.current_readings_count == 2
    assert "At least 4 reference readings" in metrics2.message


@pytest.mark.asyncio
async def test_calculate_and_apply_optimal_coefficients_end_to_end(
    db_session: AsyncSession, sample_installation: Installation
):
    """
    Test end-to-end optimization with 4 reference readings and simulated telemetry.
    Verify:
    1. calculate_optimal_coefficients returns can_optimize=True with improvement.
    2. apply_optimal_coefficients saves new parameters and recalculates deltas.
    """
    # Base reading 1: 2026-01-01 (10000, 5000)
    # Reading 2: 2026-01-11
    # Reading 3: 2026-01-21
    # Reading 4: 2026-01-31
    inst_id = sample_installation.id
    v_nom = sample_installation.nominal_voltage_v

    # Ingest hourly consumption across the 30 days (720 hours)
    start_dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    for hour_idx in range(720):
        dt = start_dt + timedelta(hours=hour_idx)
        is_night = (hour_idx % 24) < 7 or (hour_idx % 24) >= 23
        raw_kwh = 1.0 if is_night else 2.5
        v_grid = 230.0 if (hour_idx % 48) < 24 else 215.0
        v_ratio = abs(v_grid - v_nom) / v_nom

        # Let's say the real physical inverter acts with:
        # base = 0.05 (5%), idle = 0.02 kWh/h, volt = 0.08
        loss_coef = 1.0 + 0.05 + 0.08 * v_ratio
        actual_meter_import = (raw_kwh + 0.02) * loss_coef

        # Stored initial VRM records use initial installation parameters (0.03, 0.015, 0.05)
        init_loss_coef = 1.0 + 0.03 + 0.05 * v_ratio
        init_corrected = (raw_kwh + 0.015) * init_loss_coef

        h = HourlyConsumption(
            installation_id=inst_id,
            timestamp=dt,
            raw_grid_import_kwh=raw_kwh,
            grid_voltage_v=v_grid,
            is_voltage_fallback=False,
            loss_coefficient=init_loss_coef,
            corrected_grid_import_kwh=init_corrected,
            tariff_zone="NIGHT" if is_night else "DAY",
            day_kwh=0.0 if is_night else init_corrected,
            night_kwh=init_corrected if is_night else 0.0,
        )
        db_session.add(h)

    # Calculate actual cumulative day and night consumption at day 10, 20, 30
    # to simulate physical meter readings
    day_cum = 10000.0
    night_cum = 5000.0

    reading_dates = [
        (datetime(2026, 1, 11, 0, 0, 0, tzinfo=timezone.utc), 240),
        (datetime(2026, 1, 21, 0, 0, 0, tzinfo=timezone.utc), 480),
        (datetime(2026, 1, 31, 0, 0, 0, tzinfo=timezone.utc), 720),
    ]

    prev_h = 0
    for dt, target_h in reading_dates:
        for hour_idx in range(prev_h, target_h):
            is_night = (hour_idx % 24) < 7 or (hour_idx % 24) >= 23
            raw_kwh = 1.0 if is_night else 2.5
            v_grid = 230.0 if (hour_idx % 48) < 24 else 215.0
            v_ratio = abs(v_grid - v_nom) / v_nom
            loss_coef = 1.0 + 0.05 + 0.08 * v_ratio
            actual = (raw_kwh + 0.02) * loss_coef
            if is_night:
                night_cum += actual
            else:
                day_cum += actual
        prev_h = target_h

        r = MeterReading(
            installation_id=inst_id,
            timestamp=dt,
            day_reading_kwh=round(day_cum),
            night_reading_kwh=round(night_cum),
            is_reference=True,
        )
        db_session.add(r)

    await db_session.commit()

    # Pre-optimization: deltas exist due to mismatch between (0.03, 0.015, 0.05) and ground truth
    readings_before = await get_readings_with_deltas(db_session, inst_id)
    assert len(readings_before) == 4
    total_delta_before = sum(abs(r.delta_total_kwh) for r in readings_before)
    assert total_delta_before > 10.0  # Significant deltas initially

    # Run optimizer
    metrics = await calculate_optimal_coefficients(db_session, sample_installation)
    assert metrics.can_optimize is True
    assert metrics.current_readings_count == 4
    assert metrics.valid_intervals_count == 3
    assert metrics.proposed_k_base > 0.03  # Should move towards 0.05
    assert metrics.sse_after < metrics.sse_before
    assert metrics.improvement_percent > 50.0

    # Apply coefficients
    updated_records = await apply_optimal_coefficients(
        session=db_session,
        installation=sample_installation,
        loss_base_factor=metrics.proposed_k_base,
        avr_idle_loss_kwh=metrics.proposed_k_idle,
        loss_voltage_factor=metrics.proposed_k_voltage,
    )
    assert updated_records == 720
    assert sample_installation.loss_base_factor == metrics.proposed_k_base
    assert sample_installation.avr_idle_loss_kwh == metrics.proposed_k_idle

    # Post-optimization: deltas should be significantly reduced
    readings_after = await get_readings_with_deltas(db_session, inst_id)
    total_delta_after = sum(abs(r.delta_total_kwh) for r in readings_after)
    assert total_delta_after < total_delta_before
