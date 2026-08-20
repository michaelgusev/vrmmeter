from datetime import datetime, timezone
from vrmmeter.services.tariff_engine import get_tariff_zone


def test_standard_day_night_tariff():
    schedule = [
        {"zone": "NIGHT", "start": "23:00", "end": "07:00"},
        {"zone": "DAY", "start": "07:00", "end": "23:00"},
    ]

    # In UTC timezone:
    # 06:30 UTC -> NIGHT
    dt1 = datetime(2026, 6, 15, 6, 30, tzinfo=timezone.utc)
    assert get_tariff_zone(dt1, timezone_name="UTC", tariff_schedule=schedule) == "NIGHT"

    # 07:00 UTC -> DAY
    dt2 = datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc)
    assert get_tariff_zone(dt2, timezone_name="UTC", tariff_schedule=schedule) == "DAY"

    # 14:00 UTC -> DAY
    dt3 = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    assert get_tariff_zone(dt3, timezone_name="UTC", tariff_schedule=schedule) == "DAY"

    # 22:59 UTC -> DAY
    dt4 = datetime(2026, 6, 15, 22, 59, tzinfo=timezone.utc)
    assert get_tariff_zone(dt4, timezone_name="UTC", tariff_schedule=schedule) == "DAY"

    # 23:00 UTC -> NIGHT
    dt5 = datetime(2026, 6, 15, 23, 0, tzinfo=timezone.utc)
    assert get_tariff_zone(dt5, timezone_name="UTC", tariff_schedule=schedule) == "NIGHT"

    # 01:00 UTC -> NIGHT
    dt6 = datetime(2026, 6, 15, 1, 0, tzinfo=timezone.utc)
    assert get_tariff_zone(dt6, timezone_name="UTC", tariff_schedule=schedule) == "NIGHT"


def test_timezone_aware_tariff():
    # Europe/Kyiv is UTC+3 in summer (EEST)
    # At 04:30 UTC, local time in Kyiv is 07:30 -> should be DAY
    dt_utc = datetime(2026, 6, 15, 4, 30, tzinfo=timezone.utc)
    assert get_tariff_zone(dt_utc, timezone_name="Europe/Kyiv") == "DAY"

    # At 03:30 UTC, local time in Kyiv is 06:30 -> should be NIGHT
    dt_utc2 = datetime(2026, 6, 15, 3, 30, tzinfo=timezone.utc)
    assert get_tariff_zone(dt_utc2, timezone_name="Europe/Kyiv") == "NIGHT"


def test_multi_zone_custom_schedule():
    custom_schedule = [
        {"zone": "PEAK", "start": "17:00", "end": "21:00"},
        {"zone": "NIGHT", "start": "23:00", "end": "07:00"},
        {"zone": "DAY", "start": "07:00", "end": "17:00"},
        {"zone": "SHOULDER", "start": "21:00", "end": "23:00"},
    ]
    # 18:00 UTC -> PEAK
    dt_peak = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
    assert get_tariff_zone(dt_peak, timezone_name="UTC", tariff_schedule=custom_schedule) == "PEAK"

    # 21:30 UTC -> SHOULDER
    dt_shoulder = datetime(2026, 6, 15, 21, 30, tzinfo=timezone.utc)
    assert get_tariff_zone(dt_shoulder, timezone_name="UTC", tariff_schedule=custom_schedule) == "SHOULDER"
