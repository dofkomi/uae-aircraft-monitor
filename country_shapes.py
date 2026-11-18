"""تحديد الدولة من الإحداثيات محلياً (بدون شبكة وبدون مفاتيح).

يعتمد على ملف حدود الدول من Natural Earth (ملكية عامة) المخزّن في
``data/countries.geojson``. الفائدة:

* تحديد دولة **كل** طائرة في التقرير، لا دولة أحداث الانقطاع فقط.
* سرعة عالية (اختبار نقطة داخل مضلّع مع مرشّح صندوق إحداثيات) بلا استهلاك
  حصة Nominatim (طلب واحد/ثانية) ولا انتظار شبكة.
* يعمل داخل GitHub Actions بلا اعتماد على خدمة خارجية أثناء التشغيل.

الدقة: حدود بمقياس 1:50 مليون، مناسبة لتحديد الدولة لكنها ليست دقيقة عند
الحدود البرية الدقيقة؛ لذلك يبقى Nominatim متاحاً كتحقق إضافي عند الحاجة.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from urllib.request import Request, urlopen

from airports import COUNTRY_NAMES

log = logging.getLogger(__name__)

KM_PER_DEGREE = 111.32

COUNTRIES_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_50m_admin_0_countries.geojson"
)

# حلقة = قائمة (lon, lat)؛ مضلّع = قائمة حلقات (الأولى الخارجية والبقية ثقوب)
Ring = list[tuple[float, float]]
Polygon = list[Ring]


def _to_ring(raw: list) -> Ring:
    return [(float(point[0]), float(point[1])) for point in raw]


def _extract_polygons(geometry: dict) -> list[Polygon]:
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [[_to_ring(ring) for ring in coords if ring]]
    if kind == "MultiPolygon":
        return [
            [_to_ring(ring) for ring in polygon if ring]
            for polygon in coords
            if polygon
        ]
    return []


def _polygon_bbox(polygons: list[Polygon]) -> tuple[float, float, float, float]:
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    for polygon in polygons:
        for ring in polygon:
            for lon, lat in ring:
                min_lon = min(min_lon, lon)
                max_lon = max(max_lon, lon)
                min_lat = min(min_lat, lat)
                max_lat = max(max_lat, lat)
    return min_lon, min_lat, max_lon, max_lat


def _ring_contains(lon: float, lat: float, ring: Ring) -> bool:
    """اختبار «نقطة داخل مضلّع» بخوارزمية ray casting."""
    inside = False
    count = len(ring)
    if count < 3:
        return False
    previous = count - 1
    for current in range(count):
        x1, y1 = ring[current]
        x2, y2 = ring[previous]
        if (y1 > lat) != (y2 > lat):
            crossing = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < crossing:
                inside = not inside
        previous = current
    return inside


def _polygon_contains(lon: float, lat: float, polygon: Polygon) -> bool:
    if not polygon or not _ring_contains(lon, lat, polygon[0]):
        return False
    # الثقوب (بحيرات/جيوب) تُستثنى
    return not any(_ring_contains(lon, lat, hole) for hole in polygon[1:])


def _bbox_distance_km(
    lat: float, lon: float, min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> float:
    """أقصر مسافة تقريبية من النقطة إلى صندوق إحداثيات الدولة (حد أدنى مضمون)."""
    dlat = 0.0
    if lat < min_lat:
        dlat = min_lat - lat
    elif lat > max_lat:
        dlat = lat - max_lat
    dlon = 0.0
    if lon < min_lon:
        dlon = min_lon - lon
    elif lon > max_lon:
        dlon = lon - max_lon
    x = dlon * KM_PER_DEGREE * math.cos(math.radians(lat))
    y = dlat * KM_PER_DEGREE
    return math.hypot(x, y)


def _segment_distance_km(
    lat: float, lon: float, first: tuple[float, float], second: tuple[float, float]
) -> float:
    """مسافة النقطة إلى قطعة مستقيمة على سطح الأرض (إسقاط مستوٍ محلي)."""
    scale = math.cos(math.radians(lat))
    x1 = (first[0] - lon) * KM_PER_DEGREE * scale
    y1 = (first[1] - lat) * KM_PER_DEGREE
    x2 = (second[0] - lon) * KM_PER_DEGREE * scale
    y2 = (second[1] - lat) * KM_PER_DEGREE
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x1, y1)
    t = -(x1 * dx + y1 * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(x1 + t * dx, y1 + t * dy)


def flag_emoji(code: str | None) -> str:
    """علم الدولة من رمز ISO2 (حروف إقليمية)."""
    if not code or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code.upper())


class CountryShapes:
    """فهرس حدود الدول مع تخزين مؤقت للنتائج."""

    def __init__(
        self,
        cache_path: Path = Path("data/countries.geojson"),
        url: str = COUNTRIES_URL,
        enabled: bool = True,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.url = url
        self.enabled = enabled
        self._features: list[tuple[float, float, float, float, str, str, list[Polygon]]] = []
        self._memo: dict[tuple[float, float], tuple[str | None, str | None]] = {}
        self._nearest_memo: dict[tuple[float, float, float], tuple[str, str, float] | None] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        """هل البيانات متاحة فعلاً؟ (بعد محاولة التحميل الأولى)."""
        self._ensure_loaded()
        return bool(self._features)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.enabled:
            return

        raw = self._read_cache()
        if raw is None:
            raw = self._download()
            if raw:
                self._write_cache(raw)
        if not raw:
            log.warning("بيانات حدود الدول غير متاحة — سيُستخدم Nominatim/المطار بدلاً منها")
            return

        try:
            payload = json.loads(raw)
        except ValueError as exc:
            log.warning("ملف حدود الدول غير صالح: %s", exc)
            return

        for feature in payload.get("features") or []:
            props = feature.get("properties") or {}
            iso2 = str(props.get("ISO_A2") or "").strip().upper()
            if len(iso2) != 2 or iso2 == "-9":
                iso2 = str(props.get("ISO_A2_EH") or "").strip().upper()
            if len(iso2) != 2 or not iso2.isalpha():
                continue
            polygons = _extract_polygons(feature.get("geometry") or {})
            if not polygons:
                continue
            name = COUNTRY_NAMES.get(iso2) or props.get("NAME_AR") or props.get("NAME_EN") or iso2
            min_lon, min_lat, max_lon, max_lat = _polygon_bbox(polygons)
            self._features.append(
                (min_lon, min_lat, max_lon, max_lat, iso2, str(name), polygons)
            )
        log.info("تم تحميل حدود %d دولة من %s", len(self._features), self.cache_path.name)

    def _read_cache(self) -> str | None:
        try:
            return self.cache_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _write_cache(self, raw: str) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(raw, encoding="utf-8")
        except OSError as exc:
            log.debug("تعذّر حفظ حدود الدول محلياً: %s", exc)

    def _download(self) -> str | None:
        try:
            request = Request(self.url, headers={"User-Agent": "uae-aircraft-monitor/1.0"})
            with urlopen(request, timeout=90) as response:
                return response.read().decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - نتجاوز الفشل ونكمل بدون حدود
            log.warning("تعذّر تنزيل حدود الدول: %s", exc)
            return None

    # ------------------------------------------------------------------
    def lookup(
        self, lat: float | None, lon: float | None
    ) -> tuple[str | None, str | None, str]:
        """يرجع (اسم الدولة، رمز ISO2، مصدر التحديد) لنقطة جغرافية."""
        if lat is None or lon is None:
            return None, None, "unknown"

        key = (round(lat, 1), round(lon, 1))  # دقة ~11 كم، كافية للدولة
        cached = self._memo.get(key)
        if cached is not None:
            return cached[0], cached[1], "offline" if cached[0] else "unknown"

        self._ensure_loaded()
        for min_lon, min_lat, max_lon, max_lat, iso2, name, polygons in self._features:
            if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                continue
            for polygon in polygons:
                if _polygon_contains(lon, lat, polygon):
                    self._memo[key] = (name, iso2)
                    return name, iso2, "offline"

        self._memo[key] = (None, None)
        return None, None, "unknown"

    def country_label(self, lat: float | None, lon: float | None) -> str:
        """نص جاهز للعرض: «🇦🇪 الإمارات» أو «🌊 فوق المياه/غير محدد»."""
        name, code, _ = self.lookup(lat, lon)
        if name:
            flag = flag_emoji(code)
            return f"{flag} {name}".strip()
        if self.ready:
            return "🌊 فوق المياه / خارج حدود الدول"
        return "—"

    def nearest(
        self, lat: float | None, lon: float | None, max_km: float = 200.0
    ) -> tuple[str, str, float] | None:
        """أقرب دولة لنقطة خارج الحدود (فوق المياه أو قرب ساحل).

        يرجع ``(اسم الدولة، رمز ISO2، المسافة بالكيلومترات)`` أو ``None`` إذا
        لم توجد دولة ضمن ``max_km``. يُستخدم فقط عند فشل التحديد المباشر،
        ليُقال «الدولة القريبة» بدل ترك الموقع بلا دولة.
        """
        if lat is None or lon is None:
            return None
        key = (round(lat, 1), round(lon, 1), float(max_km))
        if key in self._nearest_memo:
            return self._nearest_memo[key]

        self._ensure_loaded()
        candidates: list[tuple[float, tuple]] = []
        for feature in self._features:
            min_lon, min_lat, max_lon, max_lat = feature[0], feature[1], feature[2], feature[3]
            lower_bound = _bbox_distance_km(lat, lon, min_lon, min_lat, max_lon, max_lat)
            if lower_bound <= max_km:
                candidates.append((lower_bound, feature))
        candidates.sort(key=lambda item: item[0])

        best: tuple[str, str, float] | None = None
        for lower_bound, feature in candidates:
            # صندوق الدولة حد أدنى مضمون: إن تجاوز أفضل مسافة وجدناها فلا فائدة
            if best is not None and lower_bound > best[2]:
                break
            _, _, _, _, code, name, polygons = feature
            for polygon in polygons:
                for ring in polygon:
                    if len(ring) < 2:
                        continue
                    previous = ring[-1]
                    for point in ring:
                        distance = _segment_distance_km(lat, lon, previous, point)
                        if best is None or distance < best[2]:
                            best = (name, code, distance)
                        previous = point

        # صندوق الدولة حد أدنى مضمون، لكن أقرب رأس مضلّع قد يكون أبعد من الحد
        if best is not None and best[2] > max_km:
            best = None
        self._nearest_memo[key] = best
        return best



_default: CountryShapes | None = None


def default_shapes() -> CountryShapes:
    """نسخة مشتركة تُحمّل عند أول استخدام (تقرأ الإعدادات)."""
    global _default
    if _default is None:
        from config import settings

        _default = CountryShapes(
            cache_path=settings.countries_path,
            enabled=settings.use_offline_country,
        )
    return _default
