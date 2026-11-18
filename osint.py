"""إثراء OSINT مجاني بلا مفاتيح ولا تسجيل.

المصادر المستخدمة (كلها عامة ومجانية):

1. **adsbdb.com** — ``/v0/callsign/{callsign}`` يرجع خط الطيران ومطارَي
   الإقلاع والوصول المجدولين. مفيد جداً لتحويل نداء الاستدعاء إلى مسار
   مفهوم (مثال: ``UAE1`` ← دبي → لندن)، وللمقارنة بين الوجهة المجدولة
   والهبوط الفعلي.
2. **adsb.lol** — ``/v2/hex/{icao24}`` يبحث عن الطائرة عالمياً. عند انقطاع
   الإشارة فوق منطقتنا نسأل: هل ما تزال تبث في مكان آخر؟ إن كانت تبث
   فالإشارة **لم تنقطع**، بل خرجت من نطاق مستقبلاتنا فقط. هذا يمنع
   الاستنتاج الخاطئ بأن جهاز التعريف أُغلق.

كل النتائج تُخزَّن في جدول ``osint_cache`` لتفادي تكرار الطلبات واحترام
حدود الخدمات المجانية.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from models import RouteInfo
from store import Store

log = logging.getLogger(__name__)

ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
ADSB_LOL_HEX_URL = "https://api.adsb.lol/v2/hex/{icao24}"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

ROUTE_TTL = timedelta(days=7)
ROUTE_NEGATIVE_TTL = timedelta(hours=12)
GLOBAL_TTL = timedelta(seconds=90)


def clean_callsign(callsign: str | None) -> str:
    """توحيد نداء الاستدعاء: بلا مسافات وبأحرف كبيرة."""
    return "".join((callsign or "").split()).upper()


def parse_route(payload: dict | None, callsign: str | None = None) -> RouteInfo | None:
    """يحوّل استجابة adsbdb إلى :class:`RouteInfo` (أو ``None``)."""
    if not isinstance(payload, dict):
        return None
    route = (payload.get("response") or {}).get("flightroute") or {}
    if not isinstance(route, dict) or not route:
        return None
    airline = route.get("airline") or {}
    origin = route.get("origin") or {}
    destination = route.get("destination") or {}
    if not origin and not destination:
        return None
    return RouteInfo(
        callsign=clean_callsign(callsign or route.get("callsign")) or None,
        airline=airline.get("name"),
        airline_country=airline.get("country"),
        origin_icao=origin.get("icao_code"),
        origin_iata=origin.get("iata_code"),
        origin_name=origin.get("municipality") or origin.get("name"),
        origin_country=origin.get("country_iso_name") or origin.get("country_name"),
        dest_icao=destination.get("icao_code"),
        dest_iata=destination.get("iata_code"),
        dest_name=destination.get("municipality") or destination.get("name"),
        dest_country=destination.get("country_iso_name") or destination.get("country_name"),
        source="adsbdb",
    )


def parse_global_state(payload: dict | None) -> dict | None:
    """يستخرج حالة الطائرة العالمية من استجابة adsb.lol (أو ``None``)."""
    if not isinstance(payload, dict):
        return None
    aircraft = payload.get("ac") or []
    if not aircraft:
        return None
    item = aircraft[0] if isinstance(aircraft[0], dict) else None
    if not item:
        return None
    lat, lon = item.get("lat"), item.get("lon")
    if lat is None or lon is None:
        return None
    alt = item.get("alt_baro")
    on_ground = alt == "ground"
    alt_ft = None if on_ground or alt is None else int(float(alt))
    return {
        "lat": float(lat),
        "lon": float(lon),
        "alt_ft": alt_ft,
        "on_ground": on_ground,
        "callsign": (item.get("flight") or "").strip() or None,
        "seen_at": payload.get("now"),
    }


class OsintClient:
    """عميل OSINT مع تخزين مؤقت في SQLite وحد أقصى للطلبات."""

    def __init__(
        self,
        store: Store,
        enabled: bool = True,
        user_agent: str = "uae-aircraft-monitor/1.0 (research)",
        max_routes_per_poll: int = 12,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.user_agent = user_agent
        self.max_routes_per_poll = max(1, max_routes_per_poll)
        self._memory: dict[str, tuple[datetime, RouteInfo | None]] = {}
        self._global_memory: dict[str, tuple[datetime, dict | None]] = {}
        self._requests = 0

    # ------------------------------------------------------------------
    def reset_poll_budget(self) -> None:
        self._requests = 0

    def _budget_left(self) -> bool:
        return self._requests < self.max_routes_per_poll

    # ------------------------------------------------------------------ المسار
    def cached_route(self, callsign: str | None) -> RouteInfo | None:
        """المسار من الذاكرة أو قاعدة البيانات فقط (بلا شبكة)."""
        key = clean_callsign(callsign)
        if not key:
            return None
        entry = self._memory.get(key)
        if entry:
            return entry[1]
        row = self.store.get_osint(f"route:{key}")
        route = RouteInfo.from_dict(row["payload"] if row else None)
        if row or route:
            self._memory[key] = (datetime.now(timezone.utc), route)
        return route

    async def route(self, callsign: str | None) -> RouteInfo | None:
        """مسار الرحلة من adsbdb، مع تخزين مؤقت وحد للطلبات."""
        key = clean_callsign(callsign)
        if not key or not self.enabled:
            return None
        cached = self.cached_route(key)
        if cached is not None:
            return cached
        entry = self._memory.get(key)
        if entry is not None:
            # وسم سلبي حديث: لا نعيد الطلب في نفس التشغيل
            return None
        if not self._budget_left():
            return None

        self._requests += 1
        payload = await self._get_json(ADSBDB_CALLSIGN_URL.format(callsign=key))
        route = parse_route(payload, key)
        self._memory[key] = (datetime.now(timezone.utc), route)
        self.store.save_osint(
            f"route:{key}",
            route.to_dict() if route else None,
            ttl=ROUTE_TTL if route else ROUTE_NEGATIVE_TTL,
        )
        return route

    # ------------------------------------------------------------------ عالمي
    async def global_state(self, icao24: str | None) -> dict | None:
        """أين الطائرة الآن عالمياً؟ (adsb.lol) — يكشف «خروج من التغطية»."""
        key = (icao24 or "").strip().lower()
        if not key or not self.enabled:
            return None

        now = datetime.now(timezone.utc)
        entry = self._global_memory.get(key)
        if entry and now - entry[0] < GLOBAL_TTL:
            return entry[1]
        row = self.store.get_osint(f"hex:{key}", max_age=GLOBAL_TTL)
        if row:
            state = row["payload"] if isinstance(row["payload"], dict) else None
            self._global_memory[key] = (now, state)
            return state

        payload = await self._get_json(ADSB_LOL_HEX_URL.format(icao24=key))
        state = parse_global_state(payload)
        self._global_memory[key] = (now, state)
        self.store.save_osint(f"hex:{key}", state, ttl=GLOBAL_TTL)
        return state

    # ------------------------------------------------------------------
    async def _get_json(self, url: str) -> dict | None:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT, headers=headers) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        log.debug("OSINT %s → HTTP %s", url, response.status)
                        return None
                    return await response.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.debug("فشل طلب OSINT %s: %s", url, exc)
            return None


def route_to_json(route: RouteInfo | None) -> str | None:
    """تسلسل المسار لتخزينه في صفوف الأحداث/الهبوط."""
    if route is None:
        return None
    return json.dumps(route.to_dict(), ensure_ascii=False)


def route_from_json(raw: str | None) -> RouteInfo | None:
    if not raw:
        return None
    try:
        return RouteInfo.from_dict(json.loads(raw))
    except (ValueError, TypeError):
        return None
