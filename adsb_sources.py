"""مصادر بيانات ADS-B العامة والمجانية (بلا مفاتيح مدفوعة)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

from config import settings
from models import AircraftState, SourceHealth

log = logging.getLogger(__name__)

OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)
OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
ADSBLOL_URL = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)


def _first(data: dict, *keys: str, default=None):
    """يرجع أول قيمة غير فارغة من مفاتيح متعددة (توافق مع اختلاف المصادر)."""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _clean_callsign(value) -> str | None:
    """تنظيف نداء الاستدعاء من الحشو والرموز غير الصالحة (مثل @ أو ~)."""
    if value in (None, ""):
        return None
    text = "".join(ch for ch in str(value) if ch.isalnum()).strip()
    return text or None


def _to_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ground_flag(row: dict, alt_baro) -> bool:
    """هل الطائرة على الأرض؟ adsb.lol يستخدم alt_baro="ground" لهذا الغرض."""
    if isinstance(alt_baro, str) and alt_baro.strip().lower() == "ground":
        return True
    return bool(row.get("on_ground", False))


def _alt_to_m(value) -> float | None:
    """ارتفاع ADS-B بالأقدام (أو كلمة ground) إلى أمتار."""
    if isinstance(value, str) and value.strip().lower() == "ground":
        return 0.0
    feet = _to_float(value)
    return None if feet is None else feet * 0.3048


def _kts_to_ms(value) -> float | None:
    """سرعة بالعُقد إلى م/ث."""
    knots = _to_float(value)
    return None if knots is None else knots * 0.514444


def _fpm_to_ms(value) -> float | None:
    """معدل صعود/هبوط بالقدم/دقيقة إلى م/ث."""
    fpm = _to_float(value)
    return None if fpm is None else fpm * 0.00508


class BaseSource:
    name = "base"
    min_interval = 10.0

    def __init__(self) -> None:
        self.health = SourceHealth(name=self.name, ok=False)
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def _throttle(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self.min_interval - (loop.time() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = loop.time()

    async def fetch(self) -> list[AircraftState]:
        raise NotImplementedError

    def _mark_ok(self, count: int) -> None:
        self.health.ok = True
        self.health.last_success = datetime.now(timezone.utc)
        self.health.message = ""
        self.health.aircraft_count = count

    def _mark_error(self, exc: BaseException) -> None:
        self.health.ok = False
        self.health.message = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("[%s] فشل الجلب: %s", self.name, exc)


class AdsbLolSource(BaseSource):
    """api.adsb.lol — مجاني بالكامل، لا يتطلب تسجيلاً أو مفتاح API."""

    name = "adsb.lol"
    min_interval = 5.0

    async def fetch(self) -> list[AircraftState]:
        await self._throttle()
        url = ADSBLOL_URL.format(
            lat=settings.center_lat,
            lon=settings.center_lon,
            dist=settings.radius,
        )
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self._mark_error(exc)
            return []

        # صيغة adsb.lol الحالية: المفتاح "ac" والقيم بالأقدام/العُقد.
        # حقل "now" بالمللي ثانية، لذلك نعتمد على ساعتنا المحلية مع "seen"
        # (ثوانٍ مضت على آخر رسالة) لتفادي خلط الوحدات وانحراف ساعة الخادم.
        rows = payload.get("ac") or payload.get("aircraft") or []
        local_now = int(time.time())
        result: list[AircraftState] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            icao = str(_first(row, "hex", "icao24", default="") or "").strip().lower()
            # العناوين التي تبدأ بـ ~ ليست عناوين ICAO حقيقية
            if not icao or icao.startswith("~"):
                continue

            callsign = _first(row, "flight", "callsign", default=None)
            seen = _to_float(_first(row, "seen", "seen_pos"))
            last_contact = int(local_now - seen) if seen is not None else _to_int(row.get("last_contact"))

            # adsb.lol يبلّغ عن الطائرات على الأرض بـ alt_baro = "ground"
            alt_baro = _first(row, "alt_baro", "baro_altitude")
            on_ground = _ground_flag(row, alt_baro)

            result.append(
                AircraftState(
                    icao24=icao,
                    callsign=_clean_callsign(callsign),
                    longitude=_to_float(_first(row, "lon", "longitude")),
                    latitude=_to_float(_first(row, "lat", "latitude")),
                    baro_altitude=_alt_to_m(alt_baro),
                    geo_altitude=_alt_to_m(_first(row, "alt_geom", "geo_altitude")),
                    velocity=_kts_to_ms(_first(row, "gs", "velocity", "speed")),
                    true_track=_to_float(_first(row, "track", "true_track", "heading")),
                    vertical_rate=_fpm_to_ms(_first(row, "baro_rate", "vertical_rate")),
                    squawk=str(_first(row, "squawk", default="") or "") or None,
                    last_contact=last_contact,
                    on_ground=on_ground,
                    source=self.name,
                    registration=_first(row, "r", "registration", "reg", default=None),
                    aircraft_type=_first(row, "t", "icao_type", "type", default=None),
                    model=_first(row, "model", "aircraft", default=None),
                    manufacturer=_first(row, "manufacturer", default=None),
                )
            )
        self._mark_ok(len(result))
        return result


class OpenSkySource(BaseSource):
    """OpenSky Network — مجاني (400 رصيد/يوم للمجهول، 4000 للمسجّل).

    كل استعلام ``/states/all`` يكلّف رصيداً واحداً على الأقل. مع دورة كل دقيقتين
    تُستنزف الميزانية اليومية في ساعات، فيصمت المصدر لبقية اليوم بـ429.
    لذلك: مهلة دنيا طويلة + عدّاد رصيد يومي + احترام ترويسة إعادة المحاولة.
    """

    name = "OpenSky"
    min_interval = 10.0

    def __init__(self) -> None:
        super().__init__()
        self.min_interval = float(settings.opensky_interval_seconds)
        self._token: str | None = None
        self._expires_at = 0.0
        self._day = None
        self._used = 0
        self._cooldown_until = 0.0

    def _budget_allows(self) -> bool:
        """هل تسمح ميزانية اليوم باستعلام آخر؟"""
        today = datetime.now(timezone.utc).date()
        if self._day != today:
            self._day, self._used = today, 0
        if time.time() < self._cooldown_until:
            return False
        return self._used < settings.opensky_daily_credits

    def _spend(self) -> None:
        self._used += 1

    def _cool_down(self, seconds: int, message: str) -> None:
        self._cooldown_until = time.time() + max(60, seconds)
        self._mark_error(RuntimeError(message))

    async def _get_token(self, session: aiohttp.ClientSession) -> str | None:
        if not settings.has_opensky_credentials:
            return None  # وضع مجهول
        if self._token and time.time() < self._expires_at - 30:
            return self._token
        data = {
            "grant_type": "client_credentials",
            "client_id": settings.opensky_client_id,
            "client_secret": settings.opensky_client_secret,
        }
        async with session.post(OPENSKY_TOKEN_URL, data=data) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        self._token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 1800))
        self._expires_at = time.time() + expires_in
        return self._token

    async def fetch(self) -> list[AircraftState]:
        if not self._budget_allows():
            self._mark_error(
                RuntimeError("تخطّي OpenSky: ميزانية اليوم مستنفدة أو تبريد بعد 429")
            )
            return []
        await self._throttle()
        self._spend()
        lamin, lomin, lamax, lomax = settings.bbox
        params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                token = await self._get_token(session)
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                async with session.get(OPENSKY_STATES_URL, params=params, headers=headers) as resp:
                    # 429 = نفد الرصيد المجاني؛ لا نعتبره خطأً برمجياً
                    if resp.status == 429:
                        retry = resp.headers.get("X-Rate-Limit-Retry-After-Seconds", "")
                        self._cool_down(
                            int(retry) if retry.isdigit() else 3600,
                            "انتهى الرصيد اليومي المجاني (429) — إيقاف مؤقت",
                        )
                        return []
                    resp.raise_for_status()
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            self._mark_error(exc)
            return []

        result = _parse_opensky_states(payload, self.name)
        self._mark_ok(len(result))
        return result


def _parse_opensky_states(payload: dict, source_name: str) -> list[AircraftState]:
    """يحوّل متجهات حالة OpenSky إلى هياكل موحّدة."""
    result: list[AircraftState] = []
    for row in payload.get("states") or []:
        if not isinstance(row, list) or len(row) < 15:
            continue
        icao = str(row[0] or "").strip().lower()
        if not icao:
            continue
        result.append(
            AircraftState(
                icao24=icao,
                callsign=_clean_callsign(row[1]),
                origin_country=row[2],
                last_contact=_to_int(row[4]),
                longitude=_to_float(row[5]),
                latitude=_to_float(row[6]),
                baro_altitude=_to_float(row[7]),
                on_ground=bool(row[8]),
                velocity=_to_float(row[9]),
                true_track=_to_float(row[10]),
                vertical_rate=_to_float(row[11]),
                geo_altitude=_to_float(row[13]),
                squawk=(str(row[14]) if row[14] else None),
                source=source_name,
            )
        )
    return result


class WatchlistSource(OpenSkySource):
    """تتبّع عالمي لطائرات محددة عبر OpenSky.

    استعلام ``icao24`` منفرد يستهلك رصيداً واحداً فقط مهما كان عدد الطائرات،
    ويسمح برؤية الطائرة خارج منطقة الإمارات أينما هبطت (منطق عام، بلا مسارات مُبرمجة).
    """

    name = "watchlist"

    def __init__(self) -> None:
        super().__init__()
        self.min_interval = float(settings.watchlist_interval_seconds)
        self.extra_icaos: set[str] = set()

    @property
    def icaos(self) -> list[str]:
        all_icaos = settings.uae_watch_icao24 | self.extra_icaos
        return sorted(all_icaos)[: settings.watchlist_max]

    def add(self, icao24: str) -> None:
        icao = (icao24 or "").strip().lower()
        if icao and len(icao) == 6:
            self.extra_icaos.add(icao)

    async def fetch(self) -> list[AircraftState]:
        icaos = self.icaos
        if not icaos:
            self._mark_ok(0)
            return []
        if not self._budget_allows():
            self._mark_error(
                RuntimeError("تخطّي قائمة التتبّع: ميزانية OpenSky مستنفدة")
            )
            return []
        await self._throttle()
        self._spend()
        params = [("icao24", icao) for icao in icaos]
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.get(OPENSKY_STATES_URL, params=params) as resp:
                    if resp.status == 429:
                        retry = resp.headers.get("X-Rate-Limit-Retry-After-Seconds", "")
                        self._cool_down(
                            int(retry) if retry.isdigit() else 3600,
                            "انتهى الرصيد اليومي المجاني (429) — إيقاف مؤقت",
                        )
                        return []
                    resp.raise_for_status()
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self._mark_error(exc)
            return []

        result = _parse_opensky_states(payload, self.name)
        self._mark_ok(len(result))
        return result


def build_sources() -> list[BaseSource]:
    sources: list[BaseSource] = []
    if settings.use_adsb_lol:
        sources.append(AdsbLolSource())
    if settings.use_opensky:
        sources.append(OpenSkySource())
    if settings.use_watchlist:
        sources.append(WatchlistSource())
    return sources
