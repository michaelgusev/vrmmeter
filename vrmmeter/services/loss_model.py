from datetime import datetime
from typing import Optional, Tuple
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import HourlyConsumption, Installation, TelemetryParameter


class LossCalculationResult:
    def __init__(
        self,
        raw_kwh: float,
        corrected_kwh: float,
        loss_coefficient: float,
        effective_voltage: float,
        is_voltage_fallback: bool,
    ):
        self.raw_kwh = raw_kwh
        self.corrected_kwh = corrected_kwh
        self.loss_coefficient = loss_coefficient
        self.effective_voltage = effective_voltage
        self.is_voltage_fallback = is_voltage_fallback


def calculate_hourly_loss(
    raw_kwh: float,
    grid_voltage_v: Optional[float],
    nominal_voltage_v: float = 220.0,
    avr_idle_loss_kwh: float = 0.015,
    loss_base_factor: float = 0.03,
    loss_voltage_factor: float = 0.05,
) -> LossCalculationResult:
    """
    Computes corrected energy consumption for an hour:
    E_corrected = (E_raw + k_idle) * (1 + k_base + k_voltage * (|V_grid - V_nom| / V_nom))

    If grid_voltage_v is None, falls back to nominal_voltage_v.
    """
    if grid_voltage_v is None or grid_voltage_v <= 0:
        effective_voltage = nominal_voltage_v
        is_voltage_fallback = True
    else:
        effective_voltage = float(grid_voltage_v)
        is_voltage_fallback = False

    # Voltage deviation ratio
    v_nom = max(nominal_voltage_v, 1.0)
    v_dev_ratio = abs(effective_voltage - v_nom) / v_nom

    # Loss multiplier
    loss_coef = 1.0 + loss_base_factor + (loss_voltage_factor * v_dev_ratio)

    # Corrected consumption (including idle AVR consumption)
    corrected_kwh = max(0.0, (raw_kwh + avr_idle_loss_kwh) * loss_coef)

    return LossCalculationResult(
        raw_kwh=round(raw_kwh, 4),
        corrected_kwh=round(corrected_kwh, 4),
        loss_coefficient=round(loss_coef, 5),
        effective_voltage=round(effective_voltage, 2),
        is_voltage_fallback=is_voltage_fallback,
    )


async def recalculate_installation_losses(
    session: AsyncSession,
    installation: Installation,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> int:
    """
    Recalculate corrected_grid_import_kwh, day_kwh, night_kwh, and loss_coefficient
    for existing hourly_consumption records of an installation based on the latest parameters
    and telemetry parameters.
    """
    query = select(HourlyConsumption).where(HourlyConsumption.installation_id == installation.id)
    if start_dt:
        query = query.where(HourlyConsumption.timestamp >= start_dt)
    if end_dt:
        query = query.where(HourlyConsumption.timestamp <= end_dt)

    result = await session.execute(query)
    records = result.scalars().all()

    # Pre-fetch telemetry voltage readings for these timestamps if available
    ts_list = [r.timestamp for r in records]
    voltages_map: dict[datetime, float] = {}

    if ts_list:
        tel_query = select(TelemetryParameter).where(
            TelemetryParameter.installation_id == installation.id,
            TelemetryParameter.metric_name == "grid_voltage_v",
            TelemetryParameter.timestamp.in_(ts_list),
        )
        tel_res = await session.execute(tel_query)
        for t in tel_res.scalars().all():
            voltages_map[t.timestamp] = t.metric_value

    updated_count = 0
    for record in records:
        v_grid = voltages_map.get(record.timestamp)
        loss_res = calculate_hourly_loss(
            raw_kwh=record.raw_grid_import_kwh,
            grid_voltage_v=v_grid,
            nominal_voltage_v=installation.nominal_voltage_v,
            avr_idle_loss_kwh=installation.avr_idle_loss_kwh,
            loss_base_factor=installation.loss_base_factor,
            loss_voltage_factor=installation.loss_voltage_factor,
        )

        record.grid_voltage_v = loss_res.effective_voltage
        record.is_voltage_fallback = loss_res.is_voltage_fallback
        record.loss_coefficient = loss_res.loss_coefficient
        record.corrected_grid_import_kwh = loss_res.corrected_kwh

        if record.tariff_zone.upper() == "NIGHT":
            record.night_kwh = loss_res.corrected_kwh
            record.day_kwh = 0.0
        else:
            record.day_kwh = loss_res.corrected_kwh
            record.night_kwh = 0.0

        updated_count += 1

    await session.commit()
    return updated_count
