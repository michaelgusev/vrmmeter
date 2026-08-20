from typing import List, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import Installation, SystemSetting
from vrmmeter.services.vrm_client import BaseVrmClient, VrmSiteInfo, get_vrm_client


async def get_stored_vrm_token(session: AsyncSession) -> str | None:
    """Retrieve VRM token from system_settings or None."""
    stmt = select(SystemSetting).where(SystemSetting.key == "vrm_api_token")
    res = await session.execute(stmt)
    setting = res.scalar_one_or_none()
    return setting.value if setting else None


async def discover_and_import_installations(
    session: AsyncSession, vrm_client: BaseVrmClient | None = None
) -> Tuple[int, int, List[Installation]]:
    """
    Query VRM API using configured token, fetch all visible installations,
    and upsert them into the database.

    Returns: (imported_count, updated_count, list_of_installations)
    """
    if vrm_client is None:
        token = await get_stored_vrm_token(session)
        vrm_client = get_vrm_client(token)

    # 1. Fetch user info
    user_info = await vrm_client.get_user_me()
    user_id = user_info.get("idUser", 0)

    # 2. Fetch sites
    sites = await vrm_client.get_user_installations(user_id)

    imported_count = 0
    updated_count = 0
    result_installations: List[Installation] = []

    is_client_mock = getattr(vrm_client, "is_mock", False)

    for site in sites:
        stmt = select(Installation).where(Installation.vrm_site_id == site.site_id)
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()

        is_demo_site = is_client_mock or site.site_id in ("1001", "1002")

        if existing:
            # Update metadata if changed
            existing.name = site.name
            if site.timezone and site.timezone != "UTC":
                existing.timezone = site.timezone
            result_installations.append(existing)
            updated_count += 1
        else:
            new_inst = Installation(
                name=site.name,
                vrm_site_id=site.site_id,
                timezone=site.timezone or "UTC",
                nominal_voltage_v=220.0,
                avr_idle_loss_kwh=0.015,
                loss_base_factor=0.03,
                loss_voltage_factor=0.05,
                tariff_schedule=[
                    {"zone": "NIGHT", "start": "23:00", "end": "07:00"},
                    {"zone": "DAY", "start": "07:00", "end": "23:00"},
                ],
                is_active=True,
                is_demo=is_demo_site,
            )
            session.add(new_inst)
            result_installations.append(new_inst)
            imported_count += 1

    await session.commit()
    return imported_count, updated_count, result_installations
