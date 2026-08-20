import calendar
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional
import zoneinfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import ElectricityPrice, HourlyConsumption, Installation, MeterReading
from vrmmeter.schemas import (
    DailyConsumptionItem,
    MeterCalculationResponse,
    MonthlyReportResponse,
    ReferenceReadingAudit,
    TariffCostBreakdown,
)


def ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware even if returned as naive from SQLite."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def get_effective_price(
    session: AsyncSession,
    installation_id: str,
    target_date: date,
) -> Optional[ElectricityPrice]:
    """
    Find the active electricity price record effective on or before `target_date`.
    """
    stmt = (
        select(ElectricityPrice)
        .where(
            ElectricityPrice.installation_id == installation_id,
            ElectricityPrice.effective_from <= target_date,
        )
        .order_by(ElectricityPrice.effective_from.desc())
        .limit(1)
    )
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def estimate_meter_reading(
    session: AsyncSession,
    installation_id: str,
    target_time: datetime,
) -> MeterCalculationResponse:
    """
    Calculate estimated physical meter readings (day/night counters) at a specific point in time T.
    Uses the latest preceding reference reading R_ref (T_ref <= T).
    """
    target_time_utc = ensure_utc(target_time)

    # 1. Query latest preceding reference reading
    ref_stmt = (
        select(MeterReading)
        .where(
            MeterReading.installation_id == installation_id,
            MeterReading.is_reference == True,
            MeterReading.timestamp <= target_time_utc,
        )
        .order_by(MeterReading.timestamp.desc())
        .limit(1)
    )
    ref_res = await session.execute(ref_stmt)
    ref_reading = ref_res.scalar_one_or_none()

    ref_audit: Optional[ReferenceReadingAudit] = None
    start_ts: Optional[datetime] = None
    base_day = 0.0
    base_night = 0.0
    is_extrapolated = False

    if ref_reading:
        start_ts = ref_reading.timestamp
        base_day = ref_reading.day_reading_kwh
        base_night = ref_reading.night_reading_kwh
        ref_audit = ReferenceReadingAudit(
            id=ref_reading.id,
            timestamp=ref_reading.timestamp,
            day_reading_kwh=ref_reading.day_reading_kwh,
            night_reading_kwh=ref_reading.night_reading_kwh,
            notes=ref_reading.notes,
        )
        is_extrapolated = True
    else:
        # Fallback: check if ANY reference reading exists in DB (even if after target_time)
        any_ref_stmt = (
            select(MeterReading)
            .where(
                MeterReading.installation_id == installation_id,
                MeterReading.is_reference == True,
            )
            .order_by(MeterReading.timestamp.asc())
            .limit(1)
        )
        any_ref_res = await session.execute(any_ref_stmt)
        earliest_ref = any_ref_res.scalar_one_or_none()

        if earliest_ref:
            ref_audit = ReferenceReadingAudit(
                id=earliest_ref.id,
                timestamp=earliest_ref.timestamp,
                day_reading_kwh=earliest_ref.day_reading_kwh,
                night_reading_kwh=earliest_ref.night_reading_kwh,
                notes=f"Earliest reference (from {earliest_ref.timestamp.date()})",
            )
            # Extrapolate backwards if target_time is before earliest reference
            start_ts = earliest_ref.timestamp
            # Query consumption between target_time and earliest_ref
            h_query = (
                select(HourlyConsumption)
                .where(
                    HourlyConsumption.installation_id == installation_id,
                    HourlyConsumption.timestamp >= target_time_utc,
                    HourlyConsumption.timestamp < start_ts,
                )
                .order_by(HourlyConsumption.timestamp.asc())
            )
            h_res = await session.execute(h_query)
            h_records = h_res.scalars().all()
            delta_day = sum(r.day_kwh for r in h_records)
            delta_night = sum(r.night_kwh for r in h_records)
            est_day = max(0.0, earliest_ref.day_reading_kwh - delta_day)
            est_night = max(0.0, earliest_ref.night_reading_kwh - delta_night)

            return MeterCalculationResponse(
                installation_id=installation_id,
                target_time=target_time_utc,
                reference_reading=ref_audit,
                delta_day_kwh=round(-delta_day, 2),
                delta_night_kwh=round(-delta_night, 2),
                delta_total_kwh=round(-(delta_day + delta_night), 2),
                estimated_day_reading=round(est_day, 2),
                estimated_night_reading=round(est_night, 2),
                hours_calculated=len(h_records),
                is_extrapolated_from_reference=True,
            )

    # Standard forward calculation: sum from start_ts (or earliest record) to target_time
    h_query = (
        select(HourlyConsumption)
        .where(
            HourlyConsumption.installation_id == installation_id,
            HourlyConsumption.timestamp >= (start_ts or datetime(2000, 1, 1, tzinfo=timezone.utc)),
            HourlyConsumption.timestamp < target_time_utc,
        )
        .order_by(HourlyConsumption.timestamp.asc())
    )
    h_res = await session.execute(h_query)
    h_records = h_res.scalars().all()

    delta_day = sum(r.day_kwh for r in h_records)
    delta_night = sum(r.night_kwh for r in h_records)

    est_day = base_day + delta_day
    est_night = base_night + delta_night

    return MeterCalculationResponse(
        installation_id=installation_id,
        target_time=target_time_utc,
        reference_reading=ref_audit,
        delta_day_kwh=round(delta_day, 2),
        delta_night_kwh=round(delta_night, 2),
        delta_total_kwh=round(delta_day + delta_night, 2),
        estimated_day_reading=round(est_day, 2),
        estimated_night_reading=round(est_night, 2),
        hours_calculated=len(h_records),
        is_extrapolated_from_reference=is_extrapolated,
    )


async def calculate_monthly_report(
    session: AsyncSession,
    installation: Installation,
    year: int,
    month: int,
) -> MonthlyReportResponse:
    """
    Calculate complete monthly power consumption, day/night split, billing amounts,
    and start/end physical meter readings for full or incomplete months.
    """
    try:
        tz = zoneinfo.ZoneInfo(installation.timezone)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    # Local month boundaries
    days_in_month = calendar.monthrange(year, month)[1]
    start_local = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    now_utc = datetime.now(timezone.utc)
    is_incomplete = now_utc < end_utc

    # Query hourly consumption records for this month
    h_stmt = (
        select(HourlyConsumption)
        .where(
            HourlyConsumption.installation_id == installation.id,
            HourlyConsumption.timestamp >= start_utc,
            HourlyConsumption.timestamp < end_utc,
        )
        .order_by(HourlyConsumption.timestamp.asc())
    )
    h_res = await session.execute(h_stmt)
    records = h_res.scalars().all()

    # Aggregate consumption
    raw_kwh = sum(r.raw_grid_import_kwh for r in records)
    total_kwh = sum(r.corrected_grid_import_kwh for r in records)
    day_kwh = sum(r.day_kwh for r in records)
    night_kwh = sum(r.night_kwh for r in records)
    loss_kwh = max(0.0, total_kwh - raw_kwh)

    # Get effective price for this month (as of 1st of month)
    price_rec = await get_effective_price(session, installation.id, date(year, month, 1))

    day_rate = price_rec.day_price_per_kwh if price_rec else 0.0
    night_rate = price_rec.night_price_per_kwh if price_rec else 0.0
    standing_charge_daily = price_rec.standing_charge_daily if price_rec else 0.0
    currency = price_rec.currency if price_rec else "EUR"

    # Calculate days for standing charge
    if is_incomplete and now_utc > start_utc:
        elapsed_days = max(1, (now_utc.astimezone(tz).date() - start_local.date()).days + 1)
    elif is_incomplete and now_utc <= start_utc:
        elapsed_days = 0
    else:
        elapsed_days = days_in_month

    day_cost = day_kwh * day_rate
    night_cost = night_kwh * night_rate
    standing_charge = elapsed_days * standing_charge_daily
    total_bill = day_cost + night_cost + standing_charge

    # Multi-zone breakdown
    zone_breakdown: List[TariffCostBreakdown] = [
        TariffCostBreakdown(zone="DAY", kwh=round(day_kwh, 2), rate_per_kwh=day_rate, cost=round(day_cost, 2)),
        TariffCostBreakdown(zone="NIGHT", kwh=round(night_kwh, 2), rate_per_kwh=night_rate, cost=round(night_cost, 2)),
    ]

    # Calculate meter readings at start and end of month
    start_meter_calc = await estimate_meter_reading(session, installation.id, start_utc)
    effective_end_calc_ts = min(now_utc, end_utc)
    end_meter_calc = await estimate_meter_reading(session, installation.id, effective_end_calc_ts)

    # Build daily breakdown in local timezone
    daily_groups: Dict[date, List[HourlyConsumption]] = {}
    for r in records:
        r_utc = ensure_utc(r.timestamp)
        r_local_date = r_utc.astimezone(tz).date()
        if r_local_date not in daily_groups:
            daily_groups[r_local_date] = []
        daily_groups[r_local_date].append(r)

    daily_breakdown: List[DailyConsumptionItem] = []
    for d, d_records in sorted(daily_groups.items(), key=lambda x: x[0]):
        d_day_kwh = sum(rec.day_kwh for rec in d_records)
        d_night_kwh = sum(rec.night_kwh for rec in d_records)
        d_tot_kwh = sum(rec.corrected_grid_import_kwh for rec in d_records)
        d_raw_kwh = sum(rec.raw_grid_import_kwh for rec in d_records)
        d_loss = max(0.0, d_tot_kwh - d_raw_kwh)
        avg_v = sum(rec.grid_voltage_v for rec in d_records) / len(d_records) if d_records else 220.0

        daily_breakdown.append(
            DailyConsumptionItem(
                date=d,
                day_kwh=round(d_day_kwh, 2),
                night_kwh=round(d_night_kwh, 2),
                total_kwh=round(d_tot_kwh, 2),
                raw_kwh=round(d_raw_kwh, 2),
                loss_kwh=round(d_loss, 2),
                avg_voltage_v=round(avg_v, 1),
            )
        )

    month_names = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]

    last_ref_dt = (
        end_meter_calc.reference_reading.timestamp
        if end_meter_calc.reference_reading
        else None
    )

    return MonthlyReportResponse(
        installation_id=installation.id,
        installation_name=installation.name,
        year=year,
        month=month,
        month_name=month_names[month],
        is_incomplete_month=is_incomplete,
        currency=currency,
        total_kwh=round(total_kwh, 2),
        day_kwh=round(day_kwh, 2),
        night_kwh=round(night_kwh, 2),
        raw_kwh=round(raw_kwh, 2),
        avr_and_system_loss_kwh=round(loss_kwh, 2),
        day_rate=day_rate,
        night_rate=night_rate,
        day_cost=round(day_cost, 2),
        night_cost=round(night_cost, 2),
        standing_charge=round(standing_charge, 2),
        total_bill=round(total_bill, 2),
        zone_breakdown=zone_breakdown,
        start_meter_day=start_meter_calc.estimated_day_reading if start_meter_calc.is_extrapolated_from_reference else None,
        start_meter_night=start_meter_calc.estimated_night_reading if start_meter_calc.is_extrapolated_from_reference else None,
        end_meter_day=end_meter_calc.estimated_day_reading if end_meter_calc.is_extrapolated_from_reference else None,
        end_meter_night=end_meter_calc.estimated_night_reading if end_meter_calc.is_extrapolated_from_reference else None,
        last_reference_reading_date=last_ref_dt,
        daily_breakdown=daily_breakdown,
    )
