import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vrmmeter.db.models import Installation
from vrmmeter.services.installer_importer import discover_and_import_installations
from vrmmeter.services.vrm_client import MockVrmClient, VictronVrmClient


def test_extract_user_fields_shapes():
    client = VictronVrmClient(token="dummy_token")

    # Shape 1: nested under "user" (standard VRM v2 response)
    shape1 = {
        "user": {
            "idUser": 12345,
            "name": "Mikhail Gusev",
            "email": "user@example.com",
        },
        "success": True,
    }
    res1 = client._extract_user_fields(shape1)
    assert res1["idUser"] == 12345
    assert res1["name"] == "Mikhail Gusev"
    assert res1["email"] == "user@example.com"

    # Shape 2: nested under "records"
    shape2 = {
        "records": {
            "idUser": 67890,
            "username": "mgusev",
        }
    }
    res2 = client._extract_user_fields(shape2)
    assert res2["idUser"] == 67890
    assert res2["name"] == "mgusev"

    # Shape 3: top-level idUser
    shape3 = {
        "idUser": 99999,
        "email": "direct@example.com",
    }
    res3 = client._extract_user_fields(shape3)
    assert res3["idUser"] == 99999
    assert res3["email"] == "direct@example.com"


@pytest.mark.asyncio
async def test_discover_and_import_installations(db_session: AsyncSession):
    mock_client = MockVrmClient()
    imported, updated, installations = await discover_and_import_installations(
        session=db_session, vrm_client=mock_client
    )

    assert imported == 2
    assert updated == 0
    assert len(installations) == 2

    # Check imported site 1001
    stmt = select(Installation).where(Installation.vrm_site_id == "1001")
    res = await db_session.execute(stmt)
    inst1 = res.scalar_one_or_none()
    assert inst1 is not None
    assert "Main Residence" in inst1.name
    assert inst1.timezone == "Europe/Kyiv"
    assert inst1.nominal_voltage_v == 220.0
    assert inst1.avr_idle_loss_kwh == 0.015
    assert inst1.is_demo is True
    assert len(inst1.tariff_schedule) == 2

    # Modify nominal voltage locally
    inst1.nominal_voltage_v = 230.0
    await db_session.commit()

    # Re-run discovery
    imported2, updated2, installations2 = await discover_and_import_installations(
        session=db_session, vrm_client=mock_client
    )
    assert imported2 == 0
    assert updated2 == 2

    # Verify custom nominal voltage is preserved
    stmt2 = select(Installation).where(Installation.vrm_site_id == "1001")
    res2 = await db_session.execute(stmt2)
    inst1_refreshed = res2.scalar_one_or_none()
    assert inst1_refreshed.nominal_voltage_v == 230.0
