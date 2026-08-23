from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import ElectricityPrice, Installation, MeterReading, SystemSetting, TelemetryParameter
from vrmmeter.db.session import get_db
from vrmmeter.services.calculator import calculate_monthly_report
from vrmmeter.services.installer_importer import get_stored_vrm_token

import json

templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

def to_json_filter(value):
    return json.dumps(value, default=str)

templates.env.filters["tojson"] = to_json_filter

view_router = APIRouter(include_in_schema=False)

FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36" width="36" height="36">
  <defs>
    <linearGradient id="vrm-grad" x1="0%" y1="100%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#0284c7" />
      <stop offset="100%" stop-color="#10b981" />
    </linearGradient>
  </defs>
  <rect width="36" height="36" rx="8" fill="url(#vrm-grad)" />
  <path d="M19.5 6L10 19h7l-2 11 11-14h-7.5l2-10z" fill="#ffffff" />
</svg>"""


@view_router.get("/favicon.svg")
@view_router.get("/favicon.ico")
async def favicon():
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )



async def get_all_installations(session: AsyncSession):
    stmt = select(Installation).order_by(Installation.created_at.asc())
    res = await session.execute(stmt)
    return res.scalars().all()


@view_router.get("/", response_class=HTMLResponse)
async def dashboard_view(
    request: Request,
    installation_id: str | None = Query(None),
    year: int | None = Query(None),
    month: int | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    installations = await get_all_installations(session)
    selected_inst = None
    if installation_id:
        selected_inst = next((i for i in installations if i.id == installation_id), None)
    if not selected_inst and installations:
        selected_inst = installations[0]

    import zoneinfo

    if selected_inst:
        try:
            inst_tz = zoneinfo.ZoneInfo(selected_inst.timezone)
        except Exception:
            inst_tz = timezone.utc
        now_inst = datetime.now(inst_tz)
    else:
        now_inst = datetime.now(timezone.utc)

    curr_year = year or now_inst.year
    curr_month = month or now_inst.month


    report = None
    if selected_inst:
        report = await calculate_monthly_report(
            session=session,
            installation=selected_inst,
            year=curr_year,
            month=curr_month,
        )

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "installations": installations,
            "selected_inst": selected_inst,
            "year": curr_year,
            "month": curr_month,
            "report": report,
            "active_tab": "dashboard",
        },
    )


@view_router.get("/meter-tool", response_class=HTMLResponse)
async def meter_tool_view(
    request: Request,
    installation_id: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    installations = await get_all_installations(session)
    selected_inst = None
    if installation_id:
        selected_inst = next((i for i in installations if i.id == installation_id), None)
    if not selected_inst and installations:
        selected_inst = installations[0]

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M")

    return templates.TemplateResponse(
        request=request,
        name="meter_tool.html",
        context={
            "installations": installations,
            "selected_inst": selected_inst,
            "now_iso": now_iso,
            "active_tab": "meter-tool",
        },
    )


@view_router.get("/readings", response_class=HTMLResponse)
async def readings_view(
    request: Request,
    installation_id: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    installations = await get_all_installations(session)
    selected_inst = None
    if installation_id:
        selected_inst = next((i for i in installations if i.id == installation_id), None)
    if not selected_inst and installations:
        selected_inst = installations[0]

    readings = []
    if selected_inst:
        stmt = (
            select(MeterReading)
            .where(MeterReading.installation_id == selected_inst.id)
            .order_by(MeterReading.timestamp.desc())
        )
        res = await session.execute(stmt)
        readings = res.scalars().all()

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M")

    return templates.TemplateResponse(
        request=request,
        name="readings.html",
        context={
            "installations": installations,
            "selected_inst": selected_inst,
            "readings": readings,
            "now_iso": now_iso,
            "active_tab": "readings",
        },
    )


@view_router.get("/tariffs", response_class=HTMLResponse)
async def tariffs_view(
    request: Request,
    installation_id: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    installations = await get_all_installations(session)
    selected_inst = None
    if installation_id:
        selected_inst = next((i for i in installations if i.id == installation_id), None)
    if not selected_inst and installations:
        selected_inst = installations[0]

    prices = []
    if selected_inst:
        stmt = (
            select(ElectricityPrice)
            .where(ElectricityPrice.installation_id == selected_inst.id)
            .order_by(ElectricityPrice.effective_from.desc())
        )
        res = await session.execute(stmt)
        prices = res.scalars().all()

    today_iso = datetime.now().strftime("%Y-%m-%d")

    return templates.TemplateResponse(
        request=request,
        name="tariffs.html",
        context={
            "installations": installations,
            "selected_inst": selected_inst,
            "prices": prices,
            "today_iso": today_iso,
            "active_tab": "tariffs",
        },
    )


@view_router.get("/telemetry", response_class=HTMLResponse)
async def telemetry_view(
    request: Request,
    installation_id: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    installations = await get_all_installations(session)
    selected_inst = None
    if installation_id:
        selected_inst = next((i for i in installations if i.id == installation_id), None)
    if not selected_inst and installations:
        selected_inst = installations[0]

    telemetry_records = []
    if selected_inst:
        stmt = (
            select(TelemetryParameter)
            .where(TelemetryParameter.installation_id == selected_inst.id)
            .order_by(TelemetryParameter.timestamp.desc())
            .limit(100)
        )
        res = await session.execute(stmt)
        telemetry_records = res.scalars().all()

    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M")

    return templates.TemplateResponse(
        request=request,
        name="telemetry.html",
        context={
            "installations": installations,
            "selected_inst": selected_inst,
            "telemetry_records": telemetry_records,
            "now_iso": now_iso,
            "active_tab": "telemetry",
        },
    )


@view_router.get("/settings", response_class=HTMLResponse)
async def settings_view(
    request: Request,
    installation_id: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    installations = await get_all_installations(session)
    selected_inst = None
    if installation_id:
        selected_inst = next((i for i in installations if i.id == installation_id), None)
    if not selected_inst and installations:
        selected_inst = installations[0]

    token = await get_stored_vrm_token(session)

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "installations": installations,
            "selected_inst": selected_inst,
            "vrm_token": token or "",
            "active_tab": "settings",
        },
    )
