import csv
from datetime import datetime, timezone
import io
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import HourlyConsumption, Installation, TelemetryParameter
from vrmmeter.services.loss_model import calculate_hourly_loss


def parse_iso_or_common_dt(dt_str: str) -> datetime:
    """Parse various datetime string formats into UTC datetime."""
    dt_str = dt_str.strip()
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%Y-%m-%dT%H:%M:%SZ",
        ]
        dt = None
        for fmt in formats:
            try:
                dt = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                pass
        if dt is None:
            raise ValueError(f"Unable to parse datetime string: {dt_str}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


async def ingest_telemetry_batch(
    session: AsyncSession,
    installation_id: str,
    readings: List[Dict[str, Any]],
    source: str = "external_sensor",
) -> int:
    """
    Ingests a list of telemetry readings [{timestamp, metric_name, metric_value}].
    Upserts into TelemetryParameter and updates HourlyConsumption if metric is grid_voltage_v.
    """
    # Fetch installation
    inst_stmt = select(Installation).where(Installation.id == installation_id)
    inst_res = await session.execute(inst_stmt)
    installation = inst_res.scalar_one_or_none()
    if not installation:
        raise ValueError(f"Installation {installation_id} not found")

    saved_count = 0
    voltage_updates: Dict[datetime, float] = {}

    for r in readings:
        raw_ts = r["timestamp"]
        ts = parse_iso_or_common_dt(raw_ts) if isinstance(raw_ts, str) else raw_ts
        # Align to exact hour for hourly consumption matching
        hour_ts = ts.replace(minute=0, second=0, microsecond=0)

        metric_name = str(r["metric_name"]).strip()
        metric_val = float(r["metric_value"])

        # Check existing telemetry parameter
        stmt = select(TelemetryParameter).where(
            TelemetryParameter.installation_id == installation_id,
            TelemetryParameter.timestamp == ts,
            TelemetryParameter.metric_name == metric_name,
        )
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()

        if existing:
            existing.metric_value = metric_val
            existing.source = source
        else:
            param = TelemetryParameter(
                installation_id=installation_id,
                timestamp=ts,
                metric_name=metric_name,
                metric_value=metric_val,
                source=source,
            )
            session.add(param)

        saved_count += 1

        if metric_name == "grid_voltage_v":
            voltage_updates[hour_ts] = metric_val

    # Apply voltage updates to existing HourlyConsumption records
    if voltage_updates:
        for hour_dt, v_val in voltage_updates.items():
            h_stmt = select(HourlyConsumption).where(
                HourlyConsumption.installation_id == installation_id,
                HourlyConsumption.timestamp == hour_dt,
            )
            h_res = await session.execute(h_stmt)
            h_rec = h_res.scalar_one_or_none()
            if h_rec:
                loss_res = calculate_hourly_loss(
                    raw_kwh=h_rec.raw_grid_import_kwh,
                    grid_voltage_v=v_val,
                    nominal_voltage_v=installation.nominal_voltage_v,
                    avr_idle_loss_kwh=installation.avr_idle_loss_kwh,
                    loss_base_factor=installation.loss_base_factor,
                    loss_voltage_factor=installation.loss_voltage_factor,
                )
                h_rec.grid_voltage_v = loss_res.effective_voltage
                h_rec.is_voltage_fallback = loss_res.is_voltage_fallback
                h_rec.loss_coefficient = loss_res.loss_coefficient
                h_rec.corrected_grid_import_kwh = loss_res.corrected_kwh

                if h_rec.tariff_zone.upper() == "NIGHT":
                    h_rec.night_kwh = loss_res.corrected_kwh
                    h_rec.day_kwh = 0.0
                else:
                    h_rec.day_kwh = loss_res.corrected_kwh
                    h_rec.night_kwh = 0.0

    await session.commit()
    return saved_count


async def parse_and_ingest_csv(
    session: AsyncSession,
    installation_id: str,
    csv_content: str,
    source: str = "csv_import",
) -> int:
    """
    Parse CSV file content.
    Expects columns like:
    timestamp, grid_voltage_v
    or:
    datetime, metric_name, metric_value
    """
    reader = csv.DictReader(io.StringIO(csv_content.strip()))
    readings: List[Dict[str, Any]] = []

    for row in reader:
        # Standardize keys
        clean_row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
        ts_str = clean_row.get("timestamp") or clean_row.get("datetime") or clean_row.get("date")
        if not ts_str:
            continue

        if "grid_voltage_v" in clean_row:
            readings.append(
                {
                    "timestamp": ts_str,
                    "metric_name": "grid_voltage_v",
                    "metric_value": float(clean_row["grid_voltage_v"]),
                }
            )
        elif "voltage" in clean_row:
            readings.append(
                {
                    "timestamp": ts_str,
                    "metric_name": "grid_voltage_v",
                    "metric_value": float(clean_row["voltage"]),
                }
            )
        elif "metric_name" in clean_row and "metric_value" in clean_row:
            readings.append(
                {
                    "timestamp": ts_str,
                    "metric_name": clean_row["metric_name"],
                    "metric_value": float(clean_row["metric_value"]),
                }
            )

    return await ingest_telemetry_batch(session, installation_id, readings, source=source)
