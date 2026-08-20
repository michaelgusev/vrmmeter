from datetime import date, datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import (
    ElectricityPrice,
    HourlyConsumption,
    Installation,
    MeterReading,
    SyncHistory,
    SystemSetting,
    TelemetryParameter,
)
from vrmmeter.db.session import get_db
from vrmmeter.schemas import (
    ElectricityPriceCreate,
    ElectricityPriceOut,
    InstallationCreate,
    InstallationOut,
    InstallationUpdate,
    MeterCalculationRequest,
    MeterCalculationResponse,
    MeterReadingCreate,
    MeterReadingOut,
    MonthlyReportResponse,
    SyncRequest,
    SyncResponse,
    SystemSettingOut,
    TelemetryBatchIngest,
    TelemetryParameterOut,
    VrmDiscoveryItem,
    VrmImportResponse,
    VrmTestResponse,
    VrmTokenUpdate,
)
from vrmmeter.services.calculator import calculate_monthly_report, estimate_meter_reading
from vrmmeter.services.installer_importer import (
    discover_and_import_installations,
    get_stored_vrm_token,
)
from vrmmeter.services.loss_model import recalculate_installation_losses
from vrmmeter.services.sync_service import sync_all_active_installations, sync_installation
from vrmmeter.services.telemetry_service import ingest_telemetry_batch, parse_and_ingest_csv
from vrmmeter.services.vrm_client import get_vrm_client

api_router = APIRouter(prefix="/api", tags=["API"])


# --- VRM Settings & Site Discovery ---
@api_router.get("/settings/vrm-token", response_model=Optional[SystemSettingOut])
async def get_vrm_token_setting(session: AsyncSession = Depends(get_db)):
    stmt = select(SystemSetting).where(SystemSetting.key == "vrm_api_token")
    res = await session.execute(stmt)
    setting = res.scalar_one_or_none()
    return setting


@api_router.post("/settings/vrm-token")
async def save_vrm_token(
    payload: VrmTokenUpdate, session: AsyncSession = Depends(get_db)
):
    stmt = select(SystemSetting).where(SystemSetting.key == "vrm_api_token")
    res = await session.execute(stmt)
    setting = res.scalar_one_or_none()

    if setting:
        setting.value = payload.token.strip()
    else:
        setting = SystemSetting(key="vrm_api_token", value=payload.token.strip())
        session.add(setting)

    await session.commit()
    return {"status": "saved", "message": "VRM token saved successfully"}


@api_router.post("/settings/vrm-test", response_model=VrmTestResponse)
async def test_vrm_connection(
    payload: Optional[VrmTokenUpdate] = None, session: AsyncSession = Depends(get_db)
):
    token = payload.token if payload and payload.token else await get_stored_vrm_token(session)
    client = get_vrm_client(token)

    try:
        user_info = await client.get_user_me()
        user_id = user_info.get("idUser", 0)
        user_name = user_info.get("name") or user_info.get("email")
        sites = await client.get_user_installations(user_id)

        return VrmTestResponse(
            success=True,
            message=f"Connected successfully as {user_name}",
            user_id=user_id,
            user_name=user_name,
            sites_count=len(sites),
        )
    except Exception as e:
        return VrmTestResponse(
            success=False,
            message=f"Connection failed: {str(e)}",
            sites_count=0,
        )


@api_router.post("/installations/discover", response_model=VrmImportResponse)
async def discover_and_import_vrm_sites(session: AsyncSession = Depends(get_db)):
    """Discover all sites from VRM account and auto-import them into database."""
    try:
        imported, updated, installations = await discover_and_import_installations(session)
        items: List[VrmDiscoveryItem] = [
            VrmDiscoveryItem(
                site_id=inst.vrm_site_id,
                name=inst.name,
                timezone=inst.timezone,
                already_imported=True,
            )
            for inst in installations
        ]
        return VrmImportResponse(
            success=True,
            imported_count=imported,
            updated_count=updated,
            installations=items,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to discover VRM sites: {str(e)}",
        )


# --- Installations CRUD ---
@api_router.get("/installations", response_model=List[InstallationOut])
async def list_installations(session: AsyncSession = Depends(get_db)):
    stmt = select(Installation).order_by(Installation.created_at.asc())
    res = await session.execute(stmt)
    return res.scalars().all()


@api_router.post("/installations", response_model=InstallationOut, status_code=status.HTTP_201_CREATED)
async def create_installation(
    payload: InstallationCreate, session: AsyncSession = Depends(get_db)
):
    stmt = select(Installation).where(Installation.vrm_site_id == payload.vrm_site_id)
    res = await session.execute(stmt)
    if res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Installation with VRM site ID {payload.vrm_site_id} already exists",
        )

    inst = Installation(
        name=payload.name,
        vrm_site_id=payload.vrm_site_id,
        timezone=payload.timezone,
        nominal_voltage_v=payload.nominal_voltage_v,
        avr_idle_loss_kwh=payload.avr_idle_loss_kwh,
        loss_base_factor=payload.loss_base_factor,
        loss_voltage_factor=payload.loss_voltage_factor,
        tariff_schedule=[t.model_dump() for t in payload.tariff_schedule],
        is_active=payload.is_active,
        is_demo=payload.is_demo,
    )
    session.add(inst)
    await session.commit()
    await session.refresh(inst)
    return inst


@api_router.get("/installations/{inst_id}", response_model=InstallationOut)
async def get_installation(inst_id: str, session: AsyncSession = Depends(get_db)):
    stmt = select(Installation).where(Installation.id == inst_id)
    res = await session.execute(stmt)
    inst = res.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail="Installation not found")
    return inst


@api_router.put("/installations/{inst_id}", response_model=InstallationOut)
async def update_installation(
    inst_id: str, payload: InstallationUpdate, session: AsyncSession = Depends(get_db)
):
    stmt = select(Installation).where(Installation.id == inst_id)
    res = await session.execute(stmt)
    inst = res.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail="Installation not found")

    loss_params_changed = False
    for field, val in payload.model_dump(exclude_unset=True).items():
        if field in ("nominal_voltage_v", "avr_idle_loss_kwh", "loss_base_factor", "loss_voltage_factor"):
            if getattr(inst, field) != val:
                loss_params_changed = True
        if field == "tariff_schedule" and val is not None:
            setattr(inst, field, [t.model_dump() if hasattr(t, "model_dump") else t for t in val])
        else:
            setattr(inst, field, val)

    await session.commit()
    await session.refresh(inst)

    # If loss parameters changed, recalculate historical records
    if loss_params_changed:
        await recalculate_installation_losses(session, inst)

    return inst


@api_router.delete("/installations/{inst_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_installation(inst_id: str, session: AsyncSession = Depends(get_db)):
    stmt = select(Installation).where(Installation.id == inst_id)
    res = await session.execute(stmt)
    inst = res.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail="Installation not found")

    await session.delete(inst)
    await session.commit()


# --- Electricity Prices Management ---
@api_router.get("/prices", response_model=List[ElectricityPriceOut])
async def list_prices(
    installation_id: Optional[str] = Query(None), session: AsyncSession = Depends(get_db)
):
    stmt = select(ElectricityPrice).order_by(ElectricityPrice.effective_from.desc())
    if installation_id:
        stmt = stmt.where(ElectricityPrice.installation_id == installation_id)
    res = await session.execute(stmt)
    return res.scalars().all()


@api_router.post("/prices", response_model=ElectricityPriceOut, status_code=status.HTTP_201_CREATED)
async def create_price(
    payload: ElectricityPriceCreate, session: AsyncSession = Depends(get_db)
):
    # Check if a price with same installation_id and effective_from exists
    stmt = select(ElectricityPrice).where(
        ElectricityPrice.installation_id == payload.installation_id,
        ElectricityPrice.effective_from == payload.effective_from,
    )
    res = await session.execute(stmt)
    existing = res.scalar_one_or_none()

    if existing:
        existing.day_price_per_kwh = payload.day_price_per_kwh
        existing.night_price_per_kwh = payload.night_price_per_kwh
        existing.zone_prices = payload.zone_prices
        existing.standing_charge_daily = payload.standing_charge_daily
        existing.currency = payload.currency
        await session.commit()
        await session.refresh(existing)
        return existing

    price = ElectricityPrice(
        installation_id=payload.installation_id,
        effective_from=payload.effective_from,
        day_price_per_kwh=payload.day_price_per_kwh,
        night_price_per_kwh=payload.night_price_per_kwh,
        zone_prices=payload.zone_prices,
        standing_charge_daily=payload.standing_charge_daily,
        currency=payload.currency,
    )
    session.add(price)
    await session.commit()
    await session.refresh(price)
    return price


@api_router.delete("/prices/{price_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_price(price_id: int, session: AsyncSession = Depends(get_db)):
    stmt = select(ElectricityPrice).where(ElectricityPrice.id == price_id)
    res = await session.execute(stmt)
    price = res.scalar_one_or_none()
    if not price:
        raise HTTPException(status_code=404, detail="Price record not found")
    await session.delete(price)
    await session.commit()


# --- Reference Meter Readings ---
@api_router.get("/readings", response_model=List[MeterReadingOut])
async def list_meter_readings(
    installation_id: Optional[str] = Query(None), session: AsyncSession = Depends(get_db)
):
    stmt = select(MeterReading).order_by(MeterReading.timestamp.desc())
    if installation_id:
        stmt = stmt.where(MeterReading.installation_id == installation_id)
    res = await session.execute(stmt)
    return res.scalars().all()


@api_router.post("/readings", response_model=MeterReadingOut, status_code=status.HTTP_201_CREATED)
async def create_meter_reading(
    payload: MeterReadingCreate, session: AsyncSession = Depends(get_db)
):
    reading = MeterReading(
        installation_id=payload.installation_id,
        timestamp=payload.timestamp if payload.timestamp.tzinfo else payload.timestamp.replace(tzinfo=timezone.utc),
        day_reading_kwh=payload.day_reading_kwh,
        night_reading_kwh=payload.night_reading_kwh,
        zone_readings=payload.zone_readings,
        is_reference=payload.is_reference,
        notes=payload.notes,
    )
    session.add(reading)
    await session.commit()
    await session.refresh(reading)
    return reading


@api_router.delete("/readings/{reading_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meter_reading(reading_id: int, session: AsyncSession = Depends(get_db)):
    stmt = select(MeterReading).where(MeterReading.id == reading_id)
    res = await session.execute(stmt)
    reading = res.scalar_one_or_none()
    if not reading:
        raise HTTPException(status_code=404, detail="Meter reading not found")
    await session.delete(reading)
    await session.commit()


# --- Telemetry Ingestion ---
@api_router.get("/telemetry", response_model=List[TelemetryParameterOut])
async def list_telemetry(
    installation_id: str = Query(...),
    metric_name: Optional[str] = Query(None),
    limit: int = Query(100, le=1000),
    session: AsyncSession = Depends(get_db),
):
    stmt = (
        select(TelemetryParameter)
        .where(TelemetryParameter.installation_id == installation_id)
        .order_by(TelemetryParameter.timestamp.desc())
        .limit(limit)
    )
    if metric_name:
        stmt = stmt.where(TelemetryParameter.metric_name == metric_name)
    res = await session.execute(stmt)
    return res.scalars().all()


@api_router.post("/telemetry")
async def ingest_telemetry(
    payload: TelemetryBatchIngest, session: AsyncSession = Depends(get_db)
):
    count = await ingest_telemetry_batch(
        session=session,
        installation_id=payload.installation_id,
        readings=[r.model_dump() for r in payload.readings],
    )
    return {"status": "success", "ingested_count": count}


@api_router.post("/telemetry/upload-csv")
async def upload_telemetry_csv(
    installation_id: str,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_db),
):
    content = await file.read()
    csv_text = content.decode("utf-8")
    count = await parse_and_ingest_csv(session, installation_id, csv_text)
    return {"status": "success", "imported_count": count}


# --- Calculation & Reporting ---
@api_router.post("/calculate/meter", response_model=MeterCalculationResponse)
async def calculate_meter(
    payload: MeterCalculationRequest, session: AsyncSession = Depends(get_db)
):
    return await estimate_meter_reading(
        session=session,
        installation_id=payload.installation_id,
        target_time=payload.target_time,
    )


@api_router.get("/reports/monthly", response_model=MonthlyReportResponse)
async def get_monthly_report(
    installation_id: str = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    session: AsyncSession = Depends(get_db),
):
    stmt = select(Installation).where(Installation.id == installation_id)
    res = await session.execute(stmt)
    installation = res.scalar_one_or_none()
    if not installation:
        raise HTTPException(status_code=404, detail="Installation not found")

    return await calculate_monthly_report(
        session=session,
        installation=installation,
        year=year,
        month=month,
    )


# --- Sync Controls ---
@api_router.post("/sync", response_model=SyncResponse)
async def trigger_sync(
    payload: Optional[SyncRequest] = None, session: AsyncSession = Depends(get_db)
):
    if payload and payload.installation_id:
        stmt = select(Installation).where(Installation.id == payload.installation_id)
        res = await session.execute(stmt)
        inst = res.scalar_one_or_none()
        if not inst:
            raise HTTPException(status_code=404, detail="Installation not found")

        count = await sync_installation(
            session=session,
            installation=inst,
            start_dt=payload.start_time,
            end_dt=payload.end_time,
        )
        if payload.force_recalculate_losses:
            await recalculate_installation_losses(session, inst, payload.start_time, payload.end_time)

        return SyncResponse(
            status="SUCCESS",
            message=f"Synced {count} records for {inst.name}",
            records_synced=count,
        )
    else:
        total = await sync_all_active_installations(session)
        return SyncResponse(
            status="SUCCESS",
            message=f"Synced {total} records across all active installations",
            records_synced=total,
        )


@api_router.post("/losses/recalculate")
async def trigger_recalculate_losses(
    installation_id: str, session: AsyncSession = Depends(get_db)
):
    stmt = select(Installation).where(Installation.id == installation_id)
    res = await session.execute(stmt)
    inst = res.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail="Installation not found")

    count = await recalculate_installation_losses(session, inst)
    return {"status": "success", "recalculated_records": count}
