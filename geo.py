"""تحديد الدولة التي كان فوقها آخر إشارة للطائرة.

الترتيب: حدود الدول المحلية (Natural Earth) → Nominatim إن لزم → أقرب مطار.
التحديد المحلي سريع ولا يستهلك حصة أي خدمة، ويُستخدم لدولة كل طائرة في التقرير.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from airports import AirportDB, country_name
from country_shapes import CountryShapes
from store import Store

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
MIN_INTERVAL = 1.1  # ثانية — احترام سياسة استخدام Nominatim


class GeoResolver:
    def __init__(
        self,
        store: Store,
        airports: AirportDB,
        user_agent: str = "uae-aircraft-monitor/1.0 (research)",
        enabled: bool = True,
        shapes: CountryShapes | None = None,
    ) -> None:
        self.store = store
        self.airports = airports
        self.user_agent = user_agent
        self.enabled = enabled
        self.shapes = shapes
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def lookup(self, lat: float | None, lon: float | None) -> tuple[str | None, str | None, str]:
        """يرجع (اسم الدولة، رمز الدولة، مصدر التحديد)."""
        if lat is None or lon is None:
            return None, None, "unknown"

        key = f"{lat:.1f},{lon:.1f}"  # دقة ~11 كم كافية لتحديد الدولة
        cached = self.store.get_geo(key)
        if cached:
            return cached["country"], cached["country_code"], cached["source"]

        result: tuple[str | None, str | None, str] | None = None
        # 1) تحديد محلي فوري من حدود الدول
        shapes_ready = self.shapes is not None and self.shapes.ready
        if shapes_ready:
            name, code, source = self.shapes.lookup(lat, lon)
            if name:
                result = (name, code, "offline")
        # 2) Nominatim كتحقق عند غياب نتيجة محلية (مثلاً نقطة على حدود/ساحل)
        if result is None and self.enabled:
            result = await self._nominatim(lat, lon)
        # 3) أقرب مطار كحل أخير — فقط إذا لم تتوفر حدود الدول، لأن النقطة
        #    فوق المياه لا تنتمي لدولة المطار القريب منها.
        if result is None and not shapes_ready and self.airports is not None:
            result = self._from_airport(lat, lon)
        if result is None:
            result = (None, None, "unknown")

        if result[0]:
            self.store.save_geo(
                key,
                country=result[0],
                country_code=result[1],
                source=result[2],
            )
        return result

    async def _nominatim(self, lat: float, lon: float) -> tuple[str | None, str | None, str] | None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = MIN_INTERVAL - (loop.time() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = loop.time()

        params = {
            "format": "jsonv2",
            "lat": lat,
            "lon": lon,
            "zoom": 3,
            "addressdetails": 1,
            "accept-language": "ar",
        }
        headers = {"User-Agent": self.user_agent}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(NOMINATIM_URL, params=params, headers=headers) as response:
                    if response.status != 200:
                        return None
                    payload = await response.json(content_type=None)
            address = payload.get("address") or {}
            country = address.get("country") or payload.get("name")
            code = (address.get("country_code") or "").upper() or None
            if country:
                return str(country), code, "nominatim"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, AttributeError) as exc:
            log.debug("Nominatim lookup failed: %s", exc)
        return None

    def _from_airport(self, lat: float, lon: float) -> tuple[str | None, str | None, str]:
        airport = self.airports.nearest(lat, lon, max_km=200)
        if airport and airport.country_code:
            return country_name(airport.country_code), airport.country_code, "nearest_airport"
        return None, None, "unknown"
