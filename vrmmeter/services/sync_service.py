from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import HourlyConsumption, Installation, SyncHistory, TelemetryParameter
from vrmmeter.services.installer_importer import get_stored_vrm_token
from vrmmeter.services.loss_model import calculate_hourly_loss
from vrmmeter.services.tariff_engine import get_tariff_zone
from vrmmeter.services.vrm_client import BaseVrmClient, get_vrm_client


async def sync_installation(
    session: AsyncSession,
    installation: Installation,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
    vrm_client: Optional[BaseVrmClient] = None,
) -> int:
    """
    Synchronize VRM stats for a single installation.
    If start_dt is None, incrementally syncs from last known record in DB.
    """
    now = datetime.now(timezone.utc)
    if end_dt is None:
        end_dt = now

    if start_dt is None:
        # Check latest record timestamp in DB
        stmt = select(func.max(HourlyConsumption.timestamp)).where(
            HourlyConsumption.installation_id == installation.id
        )
        res = await session.execute(stmt)
        max_ts = res.scalar()
        if max_ts:
            # Re-fetch previous 2 hours to capture updated VRM stats
            start_dt = max_ts - timedelta(hours=2)
        else:
            # Default to 30 days back
            start_dt = now - timedelta(days=30)

    if start_dt > end_dt:
        start_dt = end_dt - timedelta(hours=24)

    # Initialize sync history log
    sync_log = SyncHistory(
        installation_id=installation.id,
        sync_started_at=now,
        status="RUNNING",
        records_synced=0,
    )
    session.add(sync_log)
    await session.flush()

    from vrmmeter.services.vrm_client import MockVrmClient

    if installation.is_demo or installation.vrm_site_id in ("1001", "1002"):
        # For demo sites, always use simulated mock data to prevent invalid API calls to VRM
        vrm_client = MockVrmClient()
    elif vrm_client is None:
        token = await get_stored_vrm_token(session)
        vrm_client = get_vrm_client(token)

    try:
        # 1. Fetch hourly stats from VRM API
        records = await vrm_client.get_installation_stats(
            site_id=installation.vrm_site_id,
            start_dt=start_dt,
            end_dt=end_dt,
        )

        if not records:
            sync_log.status = "SUCCESS"
            sync_log.sync_completed_at = datetime.now(timezone.utc)
            sync_log.records_synced = 0
            await session.commit()
            return 0

        # 2. Pre-fetch external grid voltages for these timestamps
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

        # 3. Process each hourly record
        synced_count = 0
        for rec in records:
            # Check existing record
            h_stmt = select(HourlyConsumption).where(
                HourlyConsumption.installation_id == installation.id,
                HourlyConsumption.timestamp == rec.timestamp,
            )
            h_res = await session.execute(h_stmt)
            existing_h = h_res.scalar_one_or_none()

            # Determine tariff zone
            zone = get_tariff_zone(
                dt_utc=rec.timestamp,
                timezone_name=installation.timezone,
                tariff_schedule=installation.tariff_schedule,
            )

            # Look up external grid voltage
            v_grid = voltages_map.get(rec.timestamp)

            # Calculate loss and corrected consumption
            loss_res = calculate_hourly_loss(
                raw_kwh=rec.grid_import_kwh,
                grid_voltage_v=v_grid,
                nominal_voltage_v=installation.nominal_voltage_v,
                avr_idle_loss_kwh=installation.avr_idle_loss_kwh,
                loss_base_factor=installation.loss_base_factor,
                loss_voltage_factor=installation.loss_voltage_factor,
            )

            day_kwh = loss_res.corrected_kwh if zone.upper() != "NIGHT" else 0.0
            night_kwh = loss_res.corrected_kwh if zone.upper() == "NIGHT" else 0.0

            if existing_h:
                existing_h.raw_grid_import_kwh = rec.grid_import_kwh
                existing_h.raw_grid_export_kwh = rec.grid_export_kwh
                existing_h.grid_voltage_v = loss_res.effective_voltage
                existing_h.is_voltage_fallback = loss_res.is_voltage_fallback
                existing_h.loss_coefficient = loss_res.loss_coefficient
                existing_h.corrected_grid_import_kwh = loss_res.corrected_kwh
                existing_h.tariff_zone = zone
                existing_h.day_kwh = day_kwh
                existing_h.night_kwh = night_kwh
                existing_h.telemetry_snapshot = rec.telemetry or None
            else:
                new_h = HourlyConsumption(
                    installation_id=installation.id,
                    timestamp=rec.timestamp,
                    raw_grid_import_kwh=rec.grid_import_kwh,
                    raw_grid_export_kwh=rec.grid_export_kwh,
                    grid_voltage_v=loss_res.effective_voltage,
                    is_voltage_fallback=loss_res.is_voltage_fallback,
                    loss_coefficient=loss_res.loss_coefficient,
                    corrected_grid_import_kwh=loss_res.corrected_kwh,
                    tariff_zone=zone,
                    day_kwh=day_kwh,
                    night_kwh=night_kwh,
                    telemetry_snapshot=rec.telemetry or None,
                )
                session.add(new_h)

            synced_count += 1

        sync_log.status = "SUCCESS"
        sync_log.records_synced = synced_count
        sync_log.sync_completed_at = datetime.now(timezone.utc)
        await session.commit()
        return synced_count

    except Exception as e:
        sync_log.status = "FAILED"
        sync_log.error_message = str(e)
        sync_log.sync_completed_at = datetime.now(timezone.utc)
        await session.commit()
        raise


async def sync_all_active_installations(session: AsyncSession) -> int:
    """Sync all active installations."""
    stmt = select(Installation).where(Installation.is_active == True)
    res = await session.execute(stmt)
    installations = res.scalars().all()

    total_records = 0
    token = await get_stored_vrm_token(session)
    vrm_client = get_vrm_client(token)

    for inst in installations:
        try:
            client_to_use = MockVrmClient() if (inst.is_demo or inst.vrm_site_id in ("1001", "1002")) else vrm_client
            records = await sync_installation(
                session=session,
                installation=inst,
                vrm_client=client_to_use,
            )
            total_records += records
        except Exception:
            # Continue syncing remaining installations
            continue

    return total_records
