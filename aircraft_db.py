"""إثراء بيانات الطائرة من قواعد بيانات عامة مجانية (بلا مفتاح API).

المصادر:
  - adsbdb.com  : https://api.adsbdb.com/v0/aircraft/{icao24}
  - hexdb.io    : https://hexdb.io/api/v1/aircraft/{icao24}
  - البيانات المرفقة من adsb.lol تُستخدم أولاً لأنها أسرع.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from models import AircraftState
from store import Store

log = logging.getLogger(__name__)

ADSBDB_URL = "https://api.adsbdb.com/v0/aircraft/{icao24}"
HEXDB_URL = "https://hexdb.io/api/v1/aircraft/{icao24}"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)
CACHE_TTL = timedelta(days=7)
NEGATIVE_TTL = timedelta(hours=6)


class AircraftDB:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._memory: dict[str, tuple[datetime, dict | None]] = {}

    async def enrich(self, state: AircraftState) -> None:
        """يملأ التسجيل/النوع/الشركة المصنّعة في الحالة إن كانت ناقصة."""
        cached = self._get_cached(state.icao24)
        if cached is None:
            cached = await self._lookup(state.icao24)
            self._memory[state.icao24] = (datetime.now(timezone.utc), cached)

        if cached:
            state.registration = state.registration or cached.get("registration")
            state.aircraft_type = state.aircraft_type or cached.get("aircraft_type")
            state.model = state.model or cached.get("model")
            state.manufacturer = state.manufacturer or cached.get("manufacturer")

    def _get_cached(self, icao24: str) -> dict | None:
        entry = self._memory.get(icao24)
        if entry:
            fetched_at, value = entry
            ttl = NEGATIVE_TTL if value is None else CACHE_TTL
            if datetime.now(timezone.utc) - fetched_at < ttl:
                return value

        row = self.store.get_meta(icao24)
        if not row:
            return None
        value = {
            "registration": row["registration"],
            "aircraft_type": row["aircraft_type"],
            "model": row["model"],
            "manufacturer": row["manufacturer"],
        }
        self._memory[icao24] = (datetime.now(timezone.utc), value)
        return value

    async def _lookup(self, icao24: str) -> dict | None:
        data = await self._adsbdb(icao24) or await self._hexdb(icao24)
        if data and any(data.values()):
            self.store.save_meta(
                icao24,
                registration=data.get("registration"),
                aircraft_type=data.get("aircraft_type"),
                model=data.get("model"),
                manufacturer=data.get("manufacturer"),
            )
            return data
        # الوسم السلبي يُحفظ في الذاكرة فقط (انظر enrich) حتى لا نُمحي بيانات معروفة
        return None

    async def _adsbdb(self, icao24: str) -> dict | None:
        url = ADSBDB_URL.format(icao24=icao24)
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json(content_type=None)
            aircraft = (payload.get("response") or {}).get("aircraft") or {}
            return {
                "registration": aircraft.get("registration"),
                "aircraft_type": aircraft.get("type"),
                "model": aircraft.get("model"),
                "manufacturer": aircraft.get("manufacturer"),
            }
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError) as exc:
            log.debug("adsbdb lookup failed for %s: %s", icao24, exc)
            return None

    async def _hexdb(self, icao24: str) -> dict | None:
        url = HEXDB_URL.format(icao24=icao24)
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    payload = await resp.json(content_type=None)
            if not isinstance(payload, dict):
                return None
            return {
                "registration": payload.get("Registration"),
                "aircraft_type": payload.get("Type"),
                "model": payload.get("Model"),
                "manufacturer": payload.get("Manufacturer"),
            }
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.debug("hexdb lookup failed for %s: %s", icao24, exc)
            return None
