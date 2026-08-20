from datetime import datetime, time
from typing import Any, Dict, List, Optional
import zoneinfo

DEFAULT_TARIFF_SCHEDULE = [
    {"zone": "NIGHT", "start": "23:00", "end": "07:00"},
    {"zone": "DAY", "start": "07:00", "end": "23:00"},
]


def parse_time_str(time_str: str) -> time:
    """Parse 'HH:MM' string to time object."""
    parts = time_str.strip().split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return time(hour=hour, minute=minute)


def is_time_in_interval(t: time, start_str: str, end_str: str) -> bool:
    """
    Check if time `t` falls in [start, end).
    Handles intervals crossing midnight (e.g. 23:00 to 07:00).
    """
    start_t = parse_time_str(start_str)
    end_t = parse_time_str(end_str)

    if start_t <= end_t:
        # Standard interval within same day: [07:00, 23:00)
        return start_t <= t < end_t
    else:
        # Interval crossing midnight: [23:00, 24:00) U [00:00, 07:00)
        return t >= start_t or t < end_t


def get_tariff_zone(
    dt_utc: datetime,
    timezone_name: str = "UTC",
    tariff_schedule: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Determine the active tariff zone (e.g., 'DAY', 'NIGHT', 'PEAK')
    for a given UTC datetime converted into the installation's local timezone.
    """
    try:
        tz = zoneinfo.ZoneInfo(timezone_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    # Convert to installation's local time
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
    dt_local = dt_utc.astimezone(tz)
    local_time = dt_local.time()

    schedule = tariff_schedule or DEFAULT_TARIFF_SCHEDULE

    for interval in schedule:
        zone = interval.get("zone", "DAY")
        start_str = interval.get("start", "00:00")
        end_str = interval.get("end", "23:59")
        if is_time_in_interval(local_time, start_str, end_str):
            return zone.upper()

    return "DAY"
