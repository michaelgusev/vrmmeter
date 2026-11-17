import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import HourlyConsumption, Installation, MeterReading
from vrmmeter.services.loss_model import recalculate_installation_losses


def ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class ObservationData:
    """Aggregated telemetry sums for an interval (or zone in an interval)."""
    s_raw: float        # sum(raw_grid_import_kwh)
    s_volt: float       # sum(raw_grid_import_kwh * |V - V_nom| / V_nom)
    n_hours: float      # total hours count
    s_v: float          # sum(|V - V_nom| / V_nom)
    y_actual: float     # actual meter advance (kWh)
    label: str          # e.g. "Interval 1 - DAY"


@dataclass
class OptimizationMetrics:
    can_optimize: bool
    message: str
    min_readings_required: int
    current_readings_count: int
    valid_intervals_count: int
    current_k_base: float
    current_k_idle: float
    current_k_voltage: float
    proposed_k_base: float
    proposed_k_idle: float
    proposed_k_voltage: float
    sse_before: float
    sse_after: float
    rmse_before: float
    rmse_after: float
    improvement_percent: float


def solve_3x3(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """
    Solve A * x = b for 3x3 matrix using Gaussian elimination with partial pivoting.
    Returns None if singular / non-invertible.
    """
    # Clone A and b to avoid mutating input
    M = [[float(val) for val in row] for row in A]
    rhs = [float(val) for val in b]

    n = 3
    for i in range(n):
        # Find pivot
        max_row = i
        max_val = abs(M[i][i])
        for k in range(i + 1, n):
            if abs(M[k][i]) > max_val:
                max_val = abs(M[k][i])
                max_row = k

        if max_val < 1e-14:
            return None

        # Swap rows
        M[i], M[max_row] = M[max_row], M[i]
        rhs[i], rhs[max_row] = rhs[max_row], rhs[i]

        # Eliminate below
        pivot = M[i][i]
        for k in range(i + 1, n):
            factor = M[k][i] / pivot
            for j in range(i, n):
                M[k][j] -= factor * M[i][j]
            rhs[k] -= factor * rhs[i]

    # Back substitution
    x = [0.0, 0.0, 0.0]
    for i in range(n - 1, -1, -1):
        s = rhs[i]
        for j in range(i + 1, n):
            s -= M[i][j] * x[j]
        x[i] = s / M[i][i]

    return x


def predict_observation(obs: ObservationData, k_base: float, k_idle: float, k_volt: float) -> float:
    """
    Predict consumption for an aggregated observation:
    E = (1 + k_base)*s_raw + k_volt*s_volt + k_idle*(1 + k_base)*n_hours + k_idle*k_volt*s_v
    """
    return (
        (1.0 + k_base) * obs.s_raw
        + k_volt * obs.s_volt
        + k_idle * (1.0 + k_base) * obs.n_hours
        + k_idle * k_volt * obs.s_v
    )


def compute_residuals_and_jacobian(
    observations: List[ObservationData],
    k_base: float,
    k_idle: float,
    k_volt: float,
) -> Tuple[List[float], List[List[float]]]:
    """Compute residuals r_i = f_i - y_i and analytic Jacobian J_i = df_i/d(k_base, k_idle, k_volt)."""
    residuals: List[float] = []
    jacobian: List[List[float]] = []

    for obs in observations:
        pred = predict_observation(obs, k_base, k_idle, k_volt)
        r = pred - obs.y_actual
        residuals.append(r)

        # Analytic derivatives:
        # df / d(k_base) = s_raw + k_idle * n_hours
        # df / d(k_idle) = (1 + k_base) * n_hours + k_volt * s_v
        # df / d(k_volt) = s_volt + k_idle * s_v
        d_base = obs.s_raw + k_idle * obs.n_hours
        d_idle = (1.0 + k_base) * obs.n_hours + k_volt * obs.s_v
        d_volt = obs.s_volt + k_idle * obs.s_v
        jacobian.append([d_base, d_idle, d_volt])

    return residuals, jacobian


def run_least_squares(
    observations: List[ObservationData],
    init_k_base: float = 0.03,
    init_k_idle: float = 0.015,
    init_k_volt: float = 0.05,
    max_iters: int = 50,
) -> Tuple[float, float, float]:
    """
    Perform damped Levenberg-Marquardt least squares regression to find optimal
    (k_base, k_idle, k_voltage) minimizing sum(r_i^2).
    Enforces physical non-negativity bounds [0.0, 0.50] for base and idle, and [0.0, 1.0] for voltage.
    """
    k_base = max(0.0, min(0.50, init_k_base))
    k_idle = max(0.0, min(0.50, init_k_idle))
    k_volt = max(0.0, min(1.00, init_k_volt))

    residuals, _ = compute_residuals_and_jacobian(observations, k_base, k_idle, k_volt)
    current_sse = sum(r * r for r in residuals)

    lam = 1e-3  # Levenberg-Marquardt damping factor

    for _ in range(max_iters):
        residuals, jacobian = compute_residuals_and_jacobian(observations, k_base, k_idle, k_volt)
        sse = sum(r * r for r in residuals)

        # Build J^T * J (3x3) and J^T * (-r) (3x1)
        JtJ = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        Jtr = [0.0, 0.0, 0.0]

        for r, j_row in zip(residuals, jacobian):
            for a in range(3):
                for b in range(3):
                    JtJ[a][b] += j_row[a] * j_row[b]
                Jtr[a] += j_row[a] * r

        # Add damping to diagonal: JtJ[a][a] += lam * (JtJ[a][a] + 1.0)
        A = [[JtJ[a][b] for b in range(3)] for a in range(3)]
        for a in range(3):
            A[a][a] += lam * (A[a][a] + 1.0)

        rhs = [-val for val in Jtr]
        step = solve_3x3(A, rhs)

        if not step:
            lam *= 10.0
            continue

        # Candidate parameters with bounds clipping
        cand_base = max(0.0, min(0.50, k_base + step[0]))
        cand_idle = max(0.0, min(0.50, k_idle + step[1]))
        cand_volt = max(0.0, min(1.00, k_volt + step[2]))

        cand_residuals, _ = compute_residuals_and_jacobian(
            observations, cand_base, cand_idle, cand_volt
        )
        cand_sse = sum(r * r for r in cand_residuals)

        if cand_sse < sse:
            # Improvement accepted
            k_base, k_idle, k_volt = cand_base, cand_idle, cand_volt
            lam = max(1e-7, lam / 5.0)
            if abs(sse - cand_sse) < 1e-6:
                break
        else:
            # Increase damping and retry
            lam = min(1e7, lam * 10.0)

    return round(k_base, 4), round(k_idle, 4), round(k_volt, 4)


async def extract_observations_for_installation(
    session: AsyncSession,
    installation: Installation,
) -> Tuple[List[ObservationData], int, int]:
    """
    Extract calibration observations from chronological reference readings and hourly records.
    Returns: (observations, total_readings_count, valid_intervals_count)
    """
    stmt = (
        select(MeterReading)
        .where(
            MeterReading.installation_id == installation.id,
            MeterReading.is_reference == True,
        )
        .order_by(MeterReading.timestamp.asc())
    )
    res = await session.execute(stmt)
    raw_readings = res.scalars().all()

    total_readings = len(raw_readings)
    if total_readings < 2:
        return [], total_readings, 0

    min_ts = ensure_utc(raw_readings[0].timestamp)
    max_ts = ensure_utc(raw_readings[-1].timestamp)

    h_stmt = (
        select(HourlyConsumption)
        .where(
            HourlyConsumption.installation_id == installation.id,
            HourlyConsumption.timestamp >= min_ts,
            HourlyConsumption.timestamp < max_ts,
        )
        .order_by(HourlyConsumption.timestamp.asc())
    )
    h_res = await session.execute(h_stmt)
    hourly_records = h_res.scalars().all()

    v_nom = max(installation.nominal_voltage_v, 1.0)
    has_dual_tariff = any((r.night_reading_kwh or 0) > 0 for r in raw_readings)

    observations: List[ObservationData] = []
    valid_intervals_count = 0

    for idx in range(1, total_readings):
        prev_r = raw_readings[idx - 1]
        curr_r = raw_readings[idx]
        prev_ts = ensure_utc(prev_r.timestamp)
        curr_ts = ensure_utc(curr_r.timestamp)

        records_in_range = [
            h for h in hourly_records
            if prev_ts <= ensure_utc(h.timestamp) < curr_ts
        ]

        if not records_in_range:
            continue

        valid_intervals_count += 1

        delta_day = float(curr_r.day_reading_kwh - prev_r.day_reading_kwh)
        delta_night = float(curr_r.night_reading_kwh - prev_r.night_reading_kwh)
        delta_total = delta_day + delta_night

        if has_dual_tariff:
            # Observation for DAY
            day_records = [h for h in records_in_range if h.tariff_zone.upper() != "NIGHT"]
            s_raw_day = sum(h.raw_grid_import_kwh for h in day_records)
            s_volt_day = sum(
                h.raw_grid_import_kwh * abs((h.grid_voltage_v or v_nom) - v_nom) / v_nom
                for h in day_records
            )
            s_v_day = sum(
                abs((h.grid_voltage_v or v_nom) - v_nom) / v_nom
                for h in day_records
            )
            observations.append(
                ObservationData(
                    s_raw=s_raw_day,
                    s_volt=s_volt_day,
                    n_hours=float(len(day_records)),
                    s_v=s_v_day,
                    y_actual=max(0.0, delta_day),
                    label=f"Interval {idx} (Day)",
                )
            )

            # Observation for NIGHT
            night_records = [h for h in records_in_range if h.tariff_zone.upper() == "NIGHT"]
            s_raw_night = sum(h.raw_grid_import_kwh for h in night_records)
            s_volt_night = sum(
                h.raw_grid_import_kwh * abs((h.grid_voltage_v or v_nom) - v_nom) / v_nom
                for h in night_records
            )
            s_v_night = sum(
                abs((h.grid_voltage_v or v_nom) - v_nom) / v_nom
                for h in night_records
            )
            observations.append(
                ObservationData(
                    s_raw=s_raw_night,
                    s_volt=s_volt_night,
                    n_hours=float(len(night_records)),
                    s_v=s_v_night,
                    y_actual=max(0.0, delta_night),
                    label=f"Interval {idx} (Night)",
                )
            )
        else:
            # Single-rate meter: total consumption across all hours
            s_raw_tot = sum(h.raw_grid_import_kwh for h in records_in_range)
            s_volt_tot = sum(
                h.raw_grid_import_kwh * abs((h.grid_voltage_v or v_nom) - v_nom) / v_nom
                for h in records_in_range
            )
            s_v_tot = sum(
                abs((h.grid_voltage_v or v_nom) - v_nom) / v_nom
                for h in records_in_range
            )
            observations.append(
                ObservationData(
                    s_raw=s_raw_tot,
                    s_volt=s_volt_tot,
                    n_hours=float(len(records_in_range)),
                    s_v=s_v_tot,
                    y_actual=max(0.0, delta_total),
                    label=f"Interval {idx} (Total)",
                )
            )

    return observations, total_readings, valid_intervals_count


async def calculate_optimal_coefficients(
    session: AsyncSession,
    installation: Installation,
    min_readings_required: int = 4,
) -> OptimizationMetrics:
    """
    Check prerequisites and run least squares optimization for an installation.
    Returns OptimizationMetrics containing status, comparison, and preview results.
    """
    observations, total_readings, valid_intervals = await extract_observations_for_installation(
        session, installation
    )

    curr_k_base = float(installation.loss_base_factor)
    curr_k_idle = float(installation.avr_idle_loss_kwh)
    curr_k_volt = float(installation.loss_voltage_factor)

    # Validation: Need at least min_readings_required (e.g. 4) and at least 3 valid intervals
    min_intervals_required = min_readings_required - 1
    if total_readings < min_readings_required or valid_intervals < min_intervals_required or len(observations) < 3:
        return OptimizationMetrics(
            can_optimize=False,
            message=(
                f"At least {min_readings_required} reference readings spanning {min_intervals_required} "
                f"consecutive intervals with synchronized telemetry data are required to optimize the 3 loss "
                f"coefficients (K_BASE, K_IDLE, K_VOLTAGE). Currently, you have {total_readings} reference "
                f"reading(s) and {valid_intervals} interval(s) with telemetry."
            ),
            min_readings_required=min_readings_required,
            current_readings_count=total_readings,
            valid_intervals_count=valid_intervals,
            current_k_base=curr_k_base,
            current_k_idle=curr_k_idle,
            current_k_voltage=curr_k_volt,
            proposed_k_base=curr_k_base,
            proposed_k_idle=curr_k_idle,
            proposed_k_voltage=curr_k_volt,
            sse_before=0.0,
            sse_after=0.0,
            rmse_before=0.0,
            rmse_after=0.0,
            improvement_percent=0.0,
        )

    # Compute baseline SSE
    res_before, _ = compute_residuals_and_jacobian(
        observations, curr_k_base, curr_k_idle, curr_k_volt
    )
    sse_before = sum(r * r for r in res_before)
    rmse_before = math.sqrt(sse_before / len(observations)) if observations else 0.0

    # Optimize
    opt_k_base, opt_k_idle, opt_k_volt = run_least_squares(
        observations,
        init_k_base=curr_k_base,
        init_k_idle=curr_k_idle,
        init_k_volt=curr_k_volt,
    )

    res_after, _ = compute_residuals_and_jacobian(
        observations, opt_k_base, opt_k_idle, opt_k_volt
    )
    sse_after = sum(r * r for r in res_after)
    rmse_after = math.sqrt(sse_after / len(observations)) if observations else 0.0

    improvement = 0.0
    if sse_before > 1e-9:
        improvement = max(0.0, round(((sse_before - sse_after) / sse_before) * 100.0, 1))

    return OptimizationMetrics(
        can_optimize=True,
        message="Optimal loss coefficients calculated successfully.",
        min_readings_required=min_readings_required,
        current_readings_count=total_readings,
        valid_intervals_count=valid_intervals,
        current_k_base=curr_k_base,
        current_k_idle=curr_k_idle,
        current_k_voltage=curr_k_volt,
        proposed_k_base=opt_k_base,
        proposed_k_idle=opt_k_idle,
        proposed_k_voltage=opt_k_volt,
        sse_before=round(sse_before, 2),
        sse_after=round(sse_after, 2),
        rmse_before=round(rmse_before, 2),
        rmse_after=round(rmse_after, 2),
        improvement_percent=improvement,
    )


async def apply_optimal_coefficients(
    session: AsyncSession,
    installation: Installation,
    loss_base_factor: float,
    avr_idle_loss_kwh: float,
    loss_voltage_factor: float,
) -> int:
    """
    Save the newly optimized coefficients to the Installation record and recalculate
    historical hourly losses across the entire installation history.
    Returns the number of updated hourly consumption records.
    """
    installation.loss_base_factor = round(max(0.0, min(0.50, loss_base_factor)), 4)
    installation.avr_idle_loss_kwh = round(max(0.0, min(0.50, avr_idle_loss_kwh)), 4)
    installation.loss_voltage_factor = round(max(0.0, min(1.00, loss_voltage_factor)), 4)

    await session.commit()
    await session.refresh(installation)

    updated_records = await recalculate_installation_losses(session, installation)
    return updated_records
