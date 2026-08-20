from datetime import datetime, timedelta, timezone
import math
import random
from typing import Any, Dict, List, Optional
import httpx
from pydantic import BaseModel

from vrmmeter.config import settings


class VrmSiteInfo(BaseModel):
    site_id: str
    name: str
    timezone: str = "UTC"
    device_name: Optional[str] = None
    portal_id: Optional[str] = None


class VrmHourlyRecord(BaseModel):
    timestamp: datetime  # UTC start of hour
    grid_import_kwh: float
    grid_export_kwh: float = 0.0
    telemetry: Dict[str, Any] = {}


class BaseVrmClient:
    is_mock: bool = False

    async def get_user_me(self) -> Dict[str, Any]:
        raise NotImplementedError

    async def get_user_installations(self, user_id: Optional[int | str] = None) -> List[VrmSiteInfo]:
        raise NotImplementedError

    async def get_installation_stats(
        self, site_id: str, start_dt: datetime, end_dt: datetime
    ) -> List[VrmHourlyRecord]:
        raise NotImplementedError


class VictronVrmClient(BaseVrmClient):
    """Victron VRM JSON API v2 Client."""

    def __init__(self, token: str, base_url: str = settings.vrm_api_base_url, timeout: float = 30.0):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "x-authorization": f"Token {self.token}",
            "Accept": "application/json",
            "User-Agent": f"VRMMeter/{settings.app_version}",
        }

    async def get_user_me(self) -> Dict[str, Any]:
        """Fetch authenticated user profile details from VRM."""
        url = f"{self.base_url}/users/me"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self.headers)
            if resp.status_code == 401 or resp.status_code == 403:
                raise ValueError("Invalid VRM API Token or unauthorized")
            resp.raise_for_status()
            data = resp.json()
            return self._extract_user_fields(data)

    def _extract_user_fields(self, data: Any) -> Dict[str, Any]:
        """Extract idUser, name, and email from diverse VRM response structures."""
        user_obj: Dict[str, Any] = {}
        if isinstance(data, dict):
            if isinstance(data.get("user"), dict):
                user_obj = data["user"]
            elif isinstance(data.get("records"), dict):
                rec = data["records"]
                if isinstance(rec.get("user"), dict):
                    user_obj = rec["user"]
                else:
                    user_obj = rec
            else:
                user_obj = data

        id_user = (
            user_obj.get("idUser")
            or user_obj.get("id_user")
            or user_obj.get("id")
            or user_obj.get("userId")
            or (data.get("idUser") if isinstance(data, dict) else None)
            or (data.get("id") if isinstance(data, dict) else None)
        )
        name = (
            user_obj.get("name")
            or user_obj.get("username")
            or user_obj.get("firstname")
            or user_obj.get("email")
            or (data.get("name") if isinstance(data, dict) else None)
        )
        email = user_obj.get("email") or (data.get("email") if isinstance(data, dict) else None)

        try:
            parsed_id = int(id_user) if id_user is not None else 0
        except (ValueError, TypeError):
            parsed_id = id_user or 0

        return {
            "idUser": parsed_id,
            "name": name or "VRM User",
            "email": email or "",
            "user": user_obj,
            "raw": data,
        }

    async def get_user_installations(self, user_id: Optional[int | str] = None) -> List[VrmSiteInfo]:
        """
        Fetch all sites/installations visible to the user.
        Tries /users/{user_id}/installations and falls back to /users/me/installations.
        """
        urls_to_try = []
        if user_id and str(user_id) not in ("0", "None", ""):
            urls_to_try.append(f"{self.base_url}/users/{user_id}/installations")
        urls_to_try.append(f"{self.base_url}/users/me/installations")

        data = None
        last_error = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for url in urls_to_try:
                try:
                    resp = await client.get(url, headers=self.headers)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                    elif resp.status_code != 404:
                        resp.raise_for_status()
                except Exception as e:
                    last_error = e
                    continue

        if data is None:
            if last_error:
                raise last_error
            raise ValueError("Failed to retrieve installations from VRM API (404 Not Found)")

        records = data.get("records", [])
        if isinstance(records, dict):
            records = list(records.values())
        elif not isinstance(records, list):
            records = [records]

        results: List[VrmSiteInfo] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            site_id = str(item.get("idSite", item.get("id", "")))
            if not site_id:
                continue
            name = item.get("name", f"Site {site_id}")
            tz = item.get("timezone", "UTC")
            if not tz or tz == "UTC":
                tz = item.get("sys_timezone") or "UTC"
            portal_id = item.get("identifier") or item.get("portal_id")
            results.append(
                VrmSiteInfo(
                    site_id=site_id,
                    name=name,
                    timezone=tz,
                    portal_id=portal_id,
                )
            )
        return results

    async def get_installation_stats(
        self, site_id: str, start_dt: datetime, end_dt: datetime
    ) -> List[VrmHourlyRecord]:
        """
        Fetch hourly energy statistics for the given site in interval [start_dt, end_dt].
        Uses VRM v2 /installations/{site_id}/stats?type=kwh&interval=hours.
        Automatically chunks requests into 25-day windows to respect VRM API 30-day limits.
        """
        import logging
        logger = logging.getLogger("vrmmeter.vrm_client")

        # Split into <= 25-day chunks
        chunk_size = timedelta(days=25)
        all_records_map: Dict[datetime, VrmHourlyRecord] = {}

        cur_start = start_dt
        while cur_start < end_dt:
            cur_end = min(cur_start + chunk_size, end_dt)
            start_ts = int(cur_start.timestamp())
            end_ts = int(cur_end.timestamp())

            url = f"{self.base_url}/installations/{site_id}/stats"
            params = {
                "type": "kwh",
                "interval": "hours",
                "start": start_ts,
                "end": end_ts,
            }

            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, headers=self.headers, params=params)
                    resp.raise_for_status()
                    data = resp.json()

                records_obj = data.get("records", data)
                logger.info(
                    f"VRM stats API site={site_id} range=[{cur_start}..{cur_end}]: keys={list(records_obj.keys()) if isinstance(records_obj, dict) else type(records_obj)}"
                )
                chunk_records = self._parse_vrm_stats_response(records_obj, cur_start, cur_end)
                for r in chunk_records:
                    all_records_map[r.timestamp] = r
            except Exception as e:
                logger.warning(f"Failed to fetch VRM stats chunk for site {site_id} [{cur_start}..{cur_end}]: {e}")
                raise

            cur_start = cur_end

        sorted_records = sorted(all_records_map.values(), key=lambda r: r.timestamp)
        logger.info(f"Total parsed VRM hourly records for site {site_id}: {len(sorted_records)}")
        return sorted_records

    def _parse_vrm_stats_response(
        self, records_obj: Any, start_dt: datetime, end_dt: datetime
    ) -> List[VrmHourlyRecord]:
        """
        Parse VRM stats payload supporting all Victron abbreviations and structures:
        - Gc (Grid to consumer) + Gb (Grid to battery)
        - kwh (Total consumption)
        - Tg (Total to grid) or Pg + Bg
        - Case-insensitive key matching
        """
        results: Dict[datetime, VrmHourlyRecord] = {}

        def get_or_create(dt: datetime) -> VrmHourlyRecord:
            if dt not in results:
                results[dt] = VrmHourlyRecord(
                    timestamp=dt,
                    grid_import_kwh=0.0,
                    grid_export_kwh=0.0,
                    telemetry={},
                )
            return results[dt]

        if isinstance(records_obj, dict):
            # Normalize keys to lowercase
            norm_map = {k.lower(): v for k, v in records_obj.items()}

            # Helper to ingest a series of [[timestamp, value], ...]
            def parse_series(series_list: Any):
                if not isinstance(series_list, list):
                    return []
                parsed = []
                for entry in series_list:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        ts_raw, val = entry[0], entry[1]
                        if ts_raw is None or val is None:
                            continue
                        ts_sec = int(ts_raw / 1000) if ts_raw > 1e11 else int(ts_raw)
                        hour_sec = (ts_sec // 3600) * 3600
                        dt = datetime.fromtimestamp(hour_sec, tz=timezone.utc)
                        parsed.append((dt, float(val)))
                return parsed

            # Check Grid Import components: Gc (Grid to consumer), Gb (Grid to battery)
            has_matrix_import = False
            if "gc" in norm_map:
                has_matrix_import = True
                for dt, val in parse_series(norm_map["gc"]):
                    rec = get_or_create(dt)
                    rec.grid_import_kwh += max(0.0, val)

            if "gb" in norm_map:
                has_matrix_import = True
                for dt, val in parse_series(norm_map["gb"]):
                    rec = get_or_create(dt)
                    rec.grid_import_kwh += max(0.0, val)

            # Fallback grid import keys if Gc/Gb matrix not used
            if not has_matrix_import:
                for alt_key in ("kwh", "from_grid", "from_to_grid", "grid_import", "consumption", "total_consumption", "grid_history", "grid"):
                    if alt_key in norm_map:
                        for dt, val in parse_series(norm_map[alt_key]):
                            rec = get_or_create(dt)
                            rec.grid_import_kwh = max(0.0, val)
                        break

            # Grid Export: Tg (Total to grid) or Pg (PV to grid) + Bg (Battery to grid)
            if "tg" in norm_map:
                for dt, val in parse_series(norm_map["tg"]):
                    rec = get_or_create(dt)
                    rec.grid_export_kwh = max(0.0, val)
            else:
                if "pg" in norm_map:
                    for dt, val in parse_series(norm_map["pg"]):
                        rec = get_or_create(dt)
                        rec.grid_export_kwh += max(0.0, val)
                if "bg" in norm_map:
                    for dt, val in parse_series(norm_map["bg"]):
                        rec = get_or_create(dt)
                        rec.grid_export_kwh += max(0.0, val)

            # Store any other metrics into telemetry
            for k, v in norm_map.items():
                if k not in ("gc", "gb", "tg", "pg", "bg", "kwh"):
                    for dt, val in parse_series(v):
                        if dt in results:
                            results[dt].telemetry[k] = val

        elif isinstance(records_obj, list):
            for item in records_obj:
                if isinstance(item, dict):
                    ts_raw = item.get("timestamp") or item.get("time")
                    if ts_raw is None:
                        continue
                    ts_sec = int(ts_raw / 1000) if ts_raw > 1e11 else int(ts_raw)
                    hour_sec = (ts_sec // 3600) * 3600
                    dt = datetime.fromtimestamp(hour_sec, tz=timezone.utc)
                    imp = float(item.get("Gc", item.get("gc", item.get("kwh", item.get("from_grid", 0.0)))))
                    if "Gb" in item or "gb" in item:
                        imp += float(item.get("Gb", item.get("gb", 0.0)))
                    exp = float(item.get("Tg", item.get("tg", item.get("to_grid", 0.0))))
                    rec = get_or_create(dt)
                    rec.grid_import_kwh = max(0.0, imp)
                    rec.grid_export_kwh = max(0.0, exp)
        return sorted(results.values(), key=lambda r: r.timestamp)


class MockVrmClient(BaseVrmClient):
    """Mock VRM Client for offline simulation, unit testing and demonstrations."""
    is_mock: bool = True

    def __init__(self, token: str = "mock_token"):
        self.token = token

    async def get_user_me(self) -> Dict[str, Any]:
        return {
            "idUser": 99901,
            "name": "Victron User (Mock)",
            "email": "user@example.com",
            "country": "UA",
        }

    async def get_user_installations(self, user_id: Optional[int | str] = None) -> List[VrmSiteInfo]:
        return [
            VrmSiteInfo(
                site_id="1001",
                name="Main Residence (Mock)",
                timezone="Europe/Kyiv",
                device_name="Cerbo GX & MultiPlus-II 48/5000",
                portal_id="c0619ab12345",
            ),
            VrmSiteInfo(
                site_id="1002",
                name="Country Cottage (Mock)",
                timezone="Europe/Kyiv",
                device_name="EasySolar-II GX",
                portal_id="d1720bc23456",
            ),
        ]

    async def get_installation_stats(
        self, site_id: str, start_dt: datetime, end_dt: datetime
    ) -> List[VrmHourlyRecord]:
        """Generate realistic hourly consumption data."""
        # Align to hour boundaries
        start_hour = (int(start_dt.timestamp()) // 3600) * 3600
        end_hour = (int(end_dt.timestamp()) // 3600) * 3600

        results: List[VrmHourlyRecord] = []
        cur = start_hour
        while cur <= end_hour:
            dt = datetime.fromtimestamp(cur, tz=timezone.utc)
            # Simulated consumption profile with morning and evening peaks
            local_hour = dt.hour
            base_kw = 0.4 + 0.3 * math.sin((local_hour - 6) * math.pi / 12)
            if 7 <= local_hour <= 9 or 18 <= local_hour <= 22:
                base_kw += 0.8 + 0.2 * (cur % 7) / 7.0
            
            # Add subtle deterministic variation
            consumption_kwh = max(0.05, round(base_kw + 0.1 * math.cos(cur / 7200.0), 3))
            
            results.append(
                VrmHourlyRecord(
                    timestamp=dt,
                    grid_import_kwh=consumption_kwh,
                    grid_export_kwh=0.0,
                )
            )
            cur += 3600
        return results


def get_vrm_client(token: Optional[str]) -> BaseVrmClient:
    """Factory to return live VictronVrmClient or MockVrmClient if token starts with 'mock' or is empty."""
    if not token or token.strip().lower() in ("mock", "mock_token", "test", "demo", "none"):
        return MockVrmClient(token=token or "mock_token")
    return VictronVrmClient(token=token)
