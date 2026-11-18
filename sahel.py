"""تحليل المسارات غير الطبيعية ونطاق الساحل: تحليل OSINT منهجي بلا مفاتيح.

الوحدة تنفّذ ثلاث طبقات:

1. **تحديد النطاق الجغرافي** — حزام الساحل الأفريقي، دول العبور، والممرات
   البحرية (البحر الأحمر، خليج عدن، خليج غينيا).
2. **محرّك درجة الشذوذ** — يجمع مؤشرات مستقلة (انقطاع فوق الساحل، هبوط في
   مهبط صغير غير موثّق، مخالفة المسار، انحراف عن الخط المستقيم، قفزة موقع
   غير فيزيائية، غياب نداء الاستدعاء، مؤشرات عسكرية) في درجة 0–100%.
3. **مصفوفة الاستخبارات** — صف لكل طائرة مشتبه بها بالحقول التسعة المطلوبة،
   مع روابط تحقّق بصري مجانية (Sentinel Hub EO Browser وGoogle Earth).

قاعدة صارمة: لا شيء هنا يُثبت نيّة أو إغلاقاً متعمّداً لجهاز التعريف. كل
استنتاج احتمالي، ومستوى الثقة يُعلن صراحة، وما لا تعرفه البيانات يُكتب
«غير محدد» لا يُخمَّن.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models import LandingEvent, SignalEvent

# ── 1. النطاق الجغرافي ──────────────────────────────────────────────────────
# حزام الساحل الأفريقي: من المحيط الأطلسي إلى البحر الأحمر. هذه هي دول
# الساحل بالمعنى الجغرافي/السياسي المتعارف عليه (G5 Sahel + السنغال
# والسودان وإريتريا)، وليست «كل أفريقيا» ولا دول الخليج.
SAHEL_COUNTRIES = {"MR", "ML", "BF", "NE", "TD", "SD", "SN", "ER"}
# دول العبور والجوار التي تمرّ بها رحلات الشحن/التأجير قبل الدخول أو الخروج.
# منفصلة عن الساحل عمداً: تُصنَّف «دولة عبور» ولا تُحسب ضمن نطاق الساحل.
TRANSIT_COUNTRIES = {
    "LY", "DZ", "TN", "MA", "EG", "SA", "YE", "OM", "DJ", "SO", "ET", "KE",
    "UG", "SS", "CF", "CM", "NG", "BJ", "TG", "GH", "CI", "GN", "GW", "SL",
    "LR", "GM", "TZ", "RW", "BI", "CD", "CG", "GA", "GQ",
}
# أسماء عربية لعرض النطاق بوضوح في التقرير (لا رموز ISO للقارئ).
COUNTRY_AR = {
    "MR": "موريتانيا", "ML": "مالي", "BF": "بوركينا فاسو", "NE": "النيجر",
    "TD": "تشاد", "SD": "السودان", "SN": "السنغال", "ER": "إريتريا",
    "LY": "ليبيا", "DZ": "الجزائر", "TN": "تونس", "MA": "المغرب",
    "EG": "مصر", "SA": "السعودية", "YE": "اليمن", "OM": "عُمان",
    "DJ": "جيبوتي", "SO": "الصومال", "ET": "إثيوبيا", "KE": "كينيا",
    "UG": "أوغندا", "SS": "جنوب السودان", "CF": "أفريقيا الوسطى",
    "CM": "الكاميرون", "NG": "نيجيريا", "BJ": "بنين", "TG": "توغو",
    "GH": "غانا", "CI": "ساحل العاج", "GN": "غينيا", "GW": "غينيا بيساو",
    "SL": "سيراليون", "LR": "ليبيريا", "GM": "غامبيا", "TZ": "تنزانيا",
    "RW": "رواندا", "BI": "بوروندي", "CD": "الكونغو الديمقراطية",
    "CG": "الكونغو", "GA": "الغابون", "GQ": "غينيا الاستوائية",
}

# ممرات بحرية: (خط العرض الأدنى، الأعلى، خط الطول الأدنى، الأعلى)
MARITIME_CORRIDORS = {
    "البحر الأحمر": (12.0, 30.0, 32.0, 44.0),
    "خليج عدن": (10.0, 15.0, 43.0, 52.0),
    "خليج غينيا": (-1.0, 8.0, -8.0, 10.0),
}

ZONE_LABELS = {
    "sahel": "الساحل الأفريقي",
    "transit": "دولة عبور",
    "maritime": "ممر بحري",
}

# ترتيب العرض الثابت لدول الساحل (من الغرب إلى الشرق).
SAHEL_ORDER = ["MR", "SN", "ML", "BF", "NE", "TD", "SD", "ER"]

# ── عتبات هندسة المسار ──────────────────────────────────────────────────────
# أسرع طائرة نقل/مقاتلة تشغيلية دون 1,400 كم/س؛ ما فوق ذلك خطأ رصد أو تلفيق.
MAX_PLAUSIBLE_KMH = 1400.0
# لا تُقيَّم القفزات بين رصدين أقرب من دقيقة (اهتزاز GPS على الأرض والاقتراب).
MIN_TELEPORT_GAP_S = 60
# المسافة المباشرة الدنيا لقياس الالتفاف: تحتها تسود مناورات الاقتراب والانتظار.
MIN_DETOUR_BASE_KM = 150.0
# فجوة تفصل رحلتين: أطول من 45 دقيقة تعني توقّفاً على الأرض أو رحلة جديدة.
SEGMENT_GAP_S = 45 * 60
# أدنى إزاحة تُعدّ قفزة موقع حقيقية (دونها اهتزاز رصد عادي).
MIN_TELEPORT_KM = 300.0


def country_ar(code: str | None) -> str:
    """اسم الدولة بالعربية إن عُرف رمزها، وإلا الرمز نفسه."""
    if not code:
        return "غير محددة"
    return COUNTRY_AR.get(code.upper(), code.upper())


def sahel_scope_text() -> str:
    """أسماء دول الساحل الأفريقي صريحة — تُطبع في التقرير لإزالة أي التباس."""
    return "، ".join(country_ar(code) for code in SAHEL_ORDER)


# مطارات «موثّقة» تجارياً: ما دون ذلك يُعدّ مهبطاً غير موثّق يحتاج تحققاً بصرياً.
COMMERCIAL_AIRPORT_KINDS = {"large_airport", "medium_airport"}

EARTH_RADIUS_KM = 6371.0088


# ── 2. أدوات جغرافية صغيرة (بلا اعتماديات خارجية) ──────────────────────────
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """المسافة على سطح الكرة بين نقطتين بالكيلومتر."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def zone_at(
    lat: float | None, lon: float | None, country_code: str | None = None
) -> str | None:
    """يصنّف الموقع: ``sahel`` أو ``transit`` أو ``maritime`` أو ``None``."""
    code = (country_code or "").upper()
    if code in SAHEL_COUNTRIES:
        return "sahel"
    if code in TRANSIT_COUNTRIES:
        return "transit"
    if lat is None or lon is None:
        return None
    for lat_min, lat_max, lon_min, lon_max in MARITIME_CORRIDORS.values():
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return "maritime"
    return None


def zone_label(zone: str | None) -> str:
    return ZONE_LABELS.get(zone or "", "خارج نطاق التركيز")


def _deviation_km(points: list[tuple[float, float]]) -> float:
    """أقصى انحراف عن الخط المستقيم بين أول نقطة وآخرها (تقريب إقليمي)."""
    if len(points) < 4:
        return 0.0
    lat0, lon0 = points[0]
    lat1, lon1 = points[-1]
    if haversine_km(lat0, lon0, lat1, lon1) < 100:
        return 0.0
    # إسقاط مستوٍ حول متوسط خط العرض: كافٍ لمسافات إقليمية.
    mean_lat = math.radians((lat0 + lat1) / 2)
    scale = math.cos(mean_lat)

    def project(lat: float, lon: float) -> tuple[float, float]:
        return (
            math.radians(lon - lon0) * scale * EARTH_RADIUS_KM,
            math.radians(lat - lat0) * EARTH_RADIUS_KM,
        )

    ax, ay = 0.0, 0.0
    bx, by = project(lat1, lon1)
    length = math.hypot(bx - ax, by - ay)
    if length == 0:
        return 0.0
    worst = 0.0
    for lat, lon in points[1:-1]:
        px, py = project(lat, lon)
        # مسافة نقطة عن مستقيم = |cross| / |AB|
        cross = abs((bx - ax) * (ay - py) - (ax - px) * (by - ay))
        worst = max(worst, cross / length)
    return worst


def _teleport_kmh(points: list[tuple[float, float, int]]) -> tuple[float, float]:
    """(أعلى سرعة ضمنية، مسافة القفزة) — تكشف قفزات الموقع غير الفيزيائية.

    تُستبعد الفجوات القصيرة (أقل من دقيقة): اهتزاز GPS على الأرض وفي الاقتراب
    ينتج قفزات صغيرة لا معنى لها. القفزة الحقيقية تحتاج فجوة زمنية معتبرة
    وإزاحة كبيرة معاً.
    """
    fastest, jump_km = 0.0, 0.0
    for (lat1, lon1, t1), (lat2, lon2, t2) in zip(points, points[1:]):
        dt = t2 - t1
        if dt < MIN_TELEPORT_GAP_S:
            continue
        distance = haversine_km(lat1, lon1, lat2, lon2)
        speed = distance / (dt / 3600.0)
        if speed > fastest:
            fastest, jump_km = speed, distance
    return fastest, jump_km


def _segments(
    points: list[tuple[float, float, int]], max_gap_s: int = SEGMENT_GAP_S
) -> list[list[tuple[float, float, int]]]:
    """يقسّم التتبّع إلى رحلات منفصلة عند الفجوات الطويلة (توقّف/رحلة جديدة)."""
    if not points:
        return []
    segments: list[list[tuple[float, float, int]]] = [[points[0]]]
    for previous, current in zip(points, points[1:]):
        if current[2] - previous[2] > max_gap_s:
            segments.append([current])
        else:
            segments[-1].append(current)
    return segments


def _path_stats(points: list[tuple[float, float, int]]) -> tuple[float, float, float]:
    """(طول المسار، المسافة المباشرة، أقصى انحراف) بالكيلومترات لرحلة واحدة.

    طول المسار يستبعد القفزات غير الفيزيائية كي لا يُفسد حساب الالتفاف.
    """
    path = 0.0
    for (lat1, lon1, t1), (lat2, lon2, t2) in zip(points, points[1:]):
        dt = t2 - t1
        step = haversine_km(lat1, lon1, lat2, lon2)
        if dt >= MIN_TELEPORT_GAP_S and dt > 0 and step / (dt / 3600.0) > MAX_PLAUSIBLE_KMH:
            continue  # قفزة مستحيلة: لا تُحسب جزءاً من المسار
        path += step
    straight = haversine_km(points[0][0], points[0][1], points[-1][0], points[-1][1])
    deviation = _deviation_km([(lat, lon) for lat, lon, _ in points])
    return path, straight, deviation


# ── 3. محرّك درجة الشذوذ ────────────────────────────────────────────────────
@dataclass
class AnomalyAssessment:
    """نتيجة تقييم طائرة واحدة: درجة، مؤشرات مستقلة، ومستوى ثقة."""

    icao24: str
    label: str
    score: float = 0.0
    indicators: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    @property
    def percent(self) -> int:
        return int(round(self.score * 100))

    @property
    def confidence(self) -> str:
        """الثقة تُشتق من عدد المؤشرات المستقلة لا من حجم الرقم وحده."""
        count = len(self.indicators)
        if self.score >= 0.60 and count >= 3:
            return "مرتفعة"
        if self.score >= 0.35 and count >= 2:
            return "متوسطة"
        return "منخفضة"

    @property
    def level(self) -> str:
        if self.score >= 0.60:
            return "شذوذ مرجّح"
        if self.score >= 0.35:
            return "يستحق مراجعة"
        return "طبيعي"


def assess_aircraft(
    tracker,
    icao24: str,
    events: list[SignalEvent] | None = None,
    landings: list[LandingEvent] | None = None,
    since_ts: int | None = None,
) -> AnomalyAssessment:
    """يحسب درجة الشذوذ لطائرة من أحداثها وهبوطاتها ومسارها المرصود."""
    from classifier import assess as military_assess

    events = [e for e in (events or []) if e.icao24 == icao24]
    landings = [l for l in (landings or []) if l.icao24 == icao24]
    # الأحدث أولاً: الانقطاع/الهبوط الأخير هو ما يُقيَّم ويُذكر كدليل.
    events.sort(key=lambda e: e.last_seen, reverse=True)
    landings.sort(key=lambda l: l.at, reverse=True)
    state = tracker.current.get(icao24)
    label = (
        (events[0].callsign if events else None)
        or (landings[0].callsign if landings else None)
        or (state.display_name if state else icao24)
        or icao24
    )
    result = AnomalyAssessment(icao24=icao24, label=str(label).strip())

    def add(weight: float, reason: str) -> None:
        result.score += weight
        result.indicators.append(reason)

    # 3.1 انقطاع الإشارة داخل نطاق التركيز
    # خروج الطائرة من نطاق التغطية (left_coverage) ليس شذوذاً: هو حدّ مستقبلات
    # تطوعية، لا توقّف بث. لذلك يُستبعد من الدرجة ويُحصى منفصلاً في الملخّص.
    real_events = [item for item in events if item.reason != "left_coverage"]
    if real_events:
        event = real_events[0]
        zone = zone_at(event.last_lat, event.last_lon, event.country_code)
        where = event.country or zone_label(zone)
        if zone == "sahel":
            add(0.30, f"انقطاع إشارة فوق {where} (نطاق الساحل)")
        elif zone == "transit":
            add(0.18, f"انقطاع إشارة فوق {where} (دولة عبور)")
        elif zone == "maritime":
            add(0.12, f"انقطاع إشارة فوق {where}")
        if event.still_broadcasting:
            # ما تزال تبث عالمياً: الغياب محلي، فلا نُحمّله ذنب «توقف فعلي».
            result.score -= 0.10
            result.indicators.append(
                "ما تزال تبث عالمياً — الغياب على الأرجح ثغرة تغطية لا توقف بث"
            )
        result.evidence["blackout"] = {
            "lat": event.last_lat,
            "lon": event.last_lon,
            "country": event.country,
            "zone": zone,
            "minutes_ago": int(
                (datetime.now(timezone.utc) - event.last_seen).total_seconds() // 60
            ),
        }
    elif events:
        # آخر رصد عند حدّ التغطية: يُسجّل كسياق بلا درجة شذوذ.
        event = events[0]
        result.evidence["blackout"] = {
            "lat": event.last_lat,
            "lon": event.last_lon,
            "country": event.country,
            "zone": zone_at(event.last_lat, event.last_lon, event.country_code),
            "coverage_exit": True,
        }
        result.evidence["coverage_exit"] = True

    # 3.2 الهبوط في مهبط صغير/غير موثّق داخل النطاق
    for landing in landings[:1]:
        zone = zone_at(landing.latitude, landing.longitude, landing.airport_country_code)
        kind = _airport_kind(tracker, landing.airport_ident)
        commercial = kind in COMMERCIAL_AIRPORT_KINDS
        if zone == "sahel" and not commercial:
            add(0.25, f"هبوط في مهبط غير موثّق تجارياً داخل الساحل ({landing.airport_ident})")
        elif zone == "transit" and not commercial:
            add(0.12, f"هبوط في مهبط صغير بدولة عبور ({landing.airport_ident})")
        if landing.unplanned:
            add(0.18, f"توقّف غير مبرمج: {landing.unplanned_reason or 'مخالفة المسار'}")
        if landing.divergence:
            add(0.10, "هبوط خارجي تلته وجهة غير إماراتية")
        result.evidence["landing"] = {
            "airport": landing.airport_ident,
            "airport_name": landing.airport_name,
            "country": landing.airport_country_code,
            "lat": landing.latitude,
            "lon": landing.longitude,
            "zone": zone,
            "kind": kind,
        }

    # 3.3 هندسة المسار: التفاف (طول المسار مقابل المسافة المباشرة) وقفزات موقع
    if since_ts is not None and getattr(tracker, "store", None):
        rows = tracker.store.sightings_for(icao24, since_ts)
        points = [
            (float(r["lat"]), float(r["lon"]), int(r["seen_at"]))
            for r in rows
            if r.get("lat") is not None and r.get("lon") is not None
        ]
        # تُقاس الهندسة لكل رحلة على حدة: نافذة 24 ساعة قد تحوي عدة رحلات
        # ذهاباً وإياباً، ومقارنة أول نقطة بآخرها تُنتج «التفافاً» وهمياً.
        worst_ratio, worst_path, worst_straight = 0.0, 0.0, 0.0
        deviation = 0.0
        for segment in _segments(points):
            if len(segment) < 4:
                continue
            path_km, straight_km, segment_dev = _path_stats(segment)
            deviation = max(deviation, segment_dev)
            if straight_km < MIN_DETOUR_BASE_KM:
                continue  # رحلة قصيرة: مناورات الاقتراب تسود
            ratio = path_km / straight_km
            if ratio > worst_ratio:
                worst_ratio, worst_path, worst_straight = ratio, path_km, straight_km
        if worst_straight:
            result.evidence["detour_ratio"] = round(worst_ratio, 2)
            result.evidence["path_km"] = int(worst_path)
            result.evidence["straight_km"] = int(worst_straight)
            if worst_ratio >= 2.0:
                add(
                    0.18,
                    f"مسار متعرّج بشكل غير معتاد (طول المسار {worst_ratio:.1f}× "
                    "المسافة المباشرة في الرحلة نفسها)",
                )
            elif worst_ratio >= 1.6:
                add(
                    0.10,
                    f"مسار منحرف عن المباشر (طول المسار {worst_ratio:.1f}× "
                    "المسافة المباشرة)",
                )
        if deviation:
            result.evidence["deviation_km"] = int(deviation)

        fastest, jump_km = _teleport_kmh(points)
        result.evidence["implied_speed_kmh"] = int(fastest)
        if fastest > MAX_PLAUSIBLE_KMH and jump_km >= MIN_TELEPORT_KM:
            add(
                0.12,
                f"قفزة موقع غير فيزيائية ({int(jump_km)} كم في وقت قصير، سرعة "
                f"ضمنية {int(fastest)} كم/س) — احتمال خطأ رصد أو تصادم رمز Hex",
            )

    # 3.4 مؤشرات الهوية
    if state is not None:
        if not (state.callsign or "").strip():
            add(0.06, "بلا نداء استدعاء معلن")
        verdict = military_assess(state)
        if verdict["score"] >= 0.50:
            add(0.15, f"مؤشرات عسكرية ({verdict['verdict_text']})")
        result.evidence["registration"] = state.registration
        result.evidence["operator"] = state.origin_country or state.manufacturer

    result.score = max(0.0, min(1.0, result.score))
    return result


def _airport_kind(tracker, ident: str | None) -> str:
    if not ident or not getattr(tracker, "airports", None):
        return ""
    airport = tracker.airports.by_ident(ident) if hasattr(
        tracker.airports, "by_ident"
    ) else None
    return getattr(airport, "kind", "") if airport else ""


# ── 4. مصفوفة الاستخبارات ───────────────────────────────────────────────────
MATRIX_FIELDS = [
    ("hex", "Flight_Hex"),
    ("msn", "MSN"),
    ("registration", "Registration"),
    ("operator", "Declared_Operator"),
    ("leasing_layer", "Real_Leasing_Layer"),
    ("origin", "Origin"),
    ("entry_point", "Entry_Point_Sahel"),
    ("blackout", "Blackout_LatLong"),
    ("landing", "Landing_Airfield"),
    ("score", "Anomaly_Score"),
    ("indicators", "Anomaly_Indicators"),
    ("confidence", "Confidence_Level"),
    ("audited_by", "Audited_By_Model"),
    ("dedup_key", "Dedup_Key"),
]

# ADS-B العام لا يحمل الرقم التسلسلي للمصنع (MSN)؛ نعلن ذلك بدل تخمينه.
MSN_UNAVAILABLE = "غير متوفر — ADS-B العام لا يحمل MSN"
AUDIT_PENDING = "غير مُدقَّق"


def dedup_key(icao24: str | None, moment, label: str = "") -> str:
    """مفتاح إزالة التكرار ``[ICAO_Hex + Date + Entry_FIR_Timestamp]``."""
    hex_code = (icao24 or "unknown").strip().lower()
    if isinstance(moment, datetime):
        stamp = int(moment.timestamp())
        day = moment.astimezone(timezone.utc).strftime("%Y-%m-%d")
    elif isinstance(moment, (int, float)):
        stamp = int(moment)
        day = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        stamp = 0
        day = "1970-01-01"
    suffix = f"|{label}" if label else ""
    return f"{hex_code}|{day}|{stamp}{suffix}"

LEASING_CAVEAT = (
    "غير محدد — سجل ADS-B العام يكشف المشغّل المعلن فقط، وسلسلة التأجير "
    "تحتاج سجلات طيران/عقود"
)

# تسميات عربية لنوع المطار (قيم OurAirports إنجليزية في المصدر).
AIRPORT_KIND_AR = {
    "large_airport": "مطار دولي كبير",
    "medium_airport": "مطار إقليمي متوسط",
    "small_airport": "مهبط صغير",
    "heliport": "مهبط مروحيات",
    "seaplane_base": "قاعدة طائرات بحرية",
    "balloonport": "مهبط مناطيد",
    "closed": "مطار مغلق",
}


def airport_kind_ar(kind: str | None) -> str:
    """اسم نوع المطار بالعربية بدل القيمة الإنجليزية الخام."""
    if not kind:
        return "نوع غير معروف"
    return AIRPORT_KIND_AR.get(kind, kind)


@dataclass
class MatrixRow:
    """صف واحد في مصفوفة الاستخبارات (Part A) بالحقول المطلوبة."""

    icao24: str
    registration: str
    operator: str
    leasing_layer: str
    entry_point: str
    blackout: str
    landing: str
    indicators: list[str]
    confidence: str
    score: int
    zone: str
    sentinel_url: str
    earth_url: str
    zone_key: str = ""
    msn: str = MSN_UNAVAILABLE
    origin: str = "غير محدد"
    audited_by: str = AUDIT_PENDING
    dedup_key: str = ""

    def as_dict(self) -> dict:
        return {
            "Flight_Hex": self.icao24.upper(),
            "MSN": self.msn,
            "Registration": self.registration,
            "Declared_Operator": self.operator,
            "Real_Leasing_Layer": self.leasing_layer,
            "Origin": self.origin,
            "Entry_Point_Sahel": self.entry_point,
            "Blackout_LatLong": self.blackout,
            "Landing_Airfield": self.landing,
            "Anomaly_Score": self.score,
            "Anomaly_Indicators": "؛ ".join(self.indicators),
            "Confidence_Level": self.confidence,
            "Audited_By_Model": self.audited_by,
            "Dedup_Key": self.dedup_key,
            "Zone": self.zone,
            "Sentinel_EO_Browser": self.sentinel_url,
            "Google_Earth": self.earth_url,
        }


def sentinel_url(lat: float | None, lon: float | None) -> str:
    """رابط تحقّق بصري مجاني على Sentinel-2 (بلا مفتاح ولا حساب)."""
    if lat is None or lon is None:
        return ""
    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    return (
        "https://apps.sentinel-hub.com/eo-browser/"
        f"?zoom=13&lat={lat:.4f}&lng={lon:.4f}&time={start}"
        "&datasource=Sentinel-2%20L2A"
    )


def earth_url(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return ""
    return f"https://earth.google.com/web/@{lat:.4f},{lon:.4f},0a,3000d,35y,0h,0t,0r"


def _fmt_point(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "—"
    return f"{lat:.4f}, {lon:.4f}"


def build_matrix(
    tracker,
    hours: int = 24,
    limit: int = 12,
    min_score: float = 0.20,
    uae_only: bool = True,
) -> list[MatrixRow]:
    """يبني مصفوفة الاستخبارات مرتّبة بدرجة الشذوذ (الأعلى أولاً).

    ``uae_only=True`` (الافتراضي) يحصر المصفوفة في الطائرات الإماراتية/
    المستأجرة إماراتياً. البوت متخصّص فيها وحدها؛ غيرها إحصاء سياقي فقط.
    """
    from classifier import is_uae_related_event

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_ts = int(since.timestamp())
    events = tracker.events_since(since)
    landings = tracker.landings_since(since)

    candidates: dict[str, None] = {}
    for event in events:
        # الخروج من نطاق التغطية ليس شذوذاً، إلا إذا كانت الطائرة إماراتية
        # (مهمّ معرفة أين خرجت من التغطية) — تُبقى حينها للسياق فقط.
        if event.reason == "left_coverage" and not is_uae_related_event(event):
            continue
        if uae_only and not is_uae_related_event(event):
            continue
        zone = zone_at(event.last_lat, event.last_lon, event.country_code)
        if zone or is_uae_related_event(event):
            candidates[event.icao24] = None
    for landing in landings:
        if uae_only and not landing.uae_related:
            continue
        zone = zone_at(landing.latitude, landing.longitude, landing.airport_country_code)
        if zone or landing.uae_related:
            candidates[landing.icao24] = None

    rows: list[MatrixRow] = []
    for icao24 in candidates:
        assessment = assess_aircraft(tracker, icao24, events, landings, since_ts)
        zone = (
            (assessment.evidence.get("blackout") or {}).get("zone")
            or (assessment.evidence.get("landing") or {}).get("zone")
        )
        # داخل الساحل يبقى في المصفوفة مهما كانت الدرجة (نطاق التركيز)،
        # وخارج الساحل لا يدخل إلا إذا تجاوز عتبة الشذوذ.
        if assessment.score < min_score and zone != "sahel":
            continue
        rows.append(_to_row(tracker, assessment, events, landings, hours))

    rows.sort(key=_sort_key)
    return rows[:limit]


# أولوية النطاق: الساحل الأفريقي أولاً، ثم العبور، ثم الممرات، ثم خارج النطاق.
_ZONE_RANK = {"sahel": 0, "transit": 1, "maritime": 2}


def _sort_key(row: "MatrixRow") -> tuple[int, int]:
    """ترتيب المصفوفة: نطاق الساحل أولاً، ثم درجة الشذوذ تنازلياً."""
    return (_ZONE_RANK.get(row.zone_key, 3), -row.score)


def _to_row(
    tracker,
    assessment: AnomalyAssessment,
    events: list[SignalEvent],
    landings: list[LandingEvent],
    hours: int,
) -> MatrixRow:
    evidence = assessment.evidence
    blackout = evidence.get("blackout") or {}
    landing = evidence.get("landing") or {}
    state = tracker.current.get(assessment.icao24)
    own_events = sorted(
        (item for item in events if item.icao24 == assessment.icao24),
        key=lambda item: item.last_seen,
        reverse=True,
    )
    own_landings = sorted(
        (item for item in landings if item.icao24 == assessment.icao24),
        key=lambda item: item.at,
        reverse=True,
    )

    registration = (
        (state.registration if state else None)
        or next((item.registration for item in own_events if item.registration), None)
        or next((item.registration for item in own_landings if item.registration), None)
        or "غير معروف"
    )
    operator = (
        (state.origin_country if state else None)
        or (getattr(state.route, "airline", None) if state and state.route else None)
        or next(
            (
                getattr(item.route, "airline", None)
                for item in own_events
                if item.route and getattr(item.route, "airline", None)
            ),
            None,
        )
        or "غير معلن"
    )

    zone = blackout.get("zone") or landing.get("zone")
    if zone == "sahel":
        if blackout.get("lat") is not None:
            entry = (
                f"دخول مُرصد إلى الساحل — {blackout.get('country') or 'دولة ساحلية'} · "
                f"{_fmt_point(blackout.get('lat'), blackout.get('lon'))}"
            )
        else:
            entry = (
                "هبوط مُرصد داخل الساحل — "
                f"{landing.get('airport_name') or landing.get('airport') or 'مهبط غير معروف'}"
            )
    elif zone == "transit":
        entry = (
            f"لم يُرصد دخول إلى الساحل — آخر رصد في "
            f"{blackout.get('country') or landing.get('country') or 'دولة عبور'} (دولة عبور)"
        )
    elif zone == "maritime":
        entry = "لم يُرصد دخول بري — آخر رصد فوق ممر بحري"
    else:
        entry = "لم يُرصد دخول إلى الساحل"
    if evidence.get("coverage_exit"):
        entry = f"{entry} · خرجت من نطاق التغطية"

    landing_text = "—"
    if landing.get("airport") or landing.get("airport_name"):
        landing_text = (
            f"{landing.get('airport_name') or landing.get('airport')} "
            f"({landing.get('airport') or '—'} · {airport_kind_ar(landing.get('kind'))}) — "
            f"{_fmt_point(landing.get('lat'), landing.get('lon'))}"
        )

    point_lat = landing.get("lat") or blackout.get("lat")
    point_lon = landing.get("lon") or blackout.get("lon")

    # الأصل: من مسار الرحلة إن أُعلن (OSINT)، وإلا نعلن عدم التحديد.
    route = next((item.route for item in own_events if item.route), None) or (
        state.route if state else None
    )
    origin = ""
    if route is not None:
        origin = (
            getattr(route, "origin_icao", None)
            or getattr(route, "origin", None)
            or ""
        )
    origin_text = str(origin).strip() or "غير محدد — لا مسار معلن"

    # مفتاح الإزالة: تاريخ/توقيت أحدث رصد إماراتي لهذه الطائرة.
    moment = (
        own_events[0].last_seen
        if own_events
        else (own_landings[0].at if own_landings else datetime.now(timezone.utc))
    )
    key = dedup_key(assessment.icao24, moment, "MAT")

    audited_by = AUDIT_PENDING
    store = getattr(tracker, "store", None)
    if store is not None and hasattr(store, "audit_for"):
        try:
            audit = store.audit_for(assessment.icao24)
        except Exception:  # noqa: BLE001 - التدقيق ليس شرطاً للصف
            audit = None
        if audit and audit.get("model"):
            audited_by = str(audit["model"])

    return MatrixRow(
        icao24=assessment.icao24,
        registration=str(registration),
        operator=str(operator),
        leasing_layer=LEASING_CAVEAT,
        entry_point=entry,
        blackout=_fmt_point(blackout.get("lat"), blackout.get("lon")),
        landing=landing_text,
        indicators=list(assessment.indicators),
        confidence=assessment.confidence,
        score=assessment.percent,
        zone=zone_label(zone),
        sentinel_url=sentinel_url(point_lat, point_lon),
        earth_url=earth_url(point_lat, point_lon),
        zone_key=zone or "",
        msn=MSN_UNAVAILABLE,
        origin=origin_text,
        audited_by=audited_by,
        dedup_key=key,
    )


def export_csv(rows: list[MatrixRow], path: Path) -> Path:
    """يصدّر المصفوفة كـ CSV للتحليل في Pandas (حقول Part A + الملحقات)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = list(dict.fromkeys(
        [label for _, label in MATRIX_FIELDS]
        + ["Zone", "Sentinel_EO_Browser", "Google_Earth"]
    ))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            data = row.as_dict()
            writer.writerow({key: data.get(key, "") for key in headers})
    return path


def summarise(tracker, hours: int = 24, rows: list[MatrixRow] | None = None) -> dict:
    """ملخّص نصي/رقمي لنطاق الساحل يُغذّي الدراسة والملفات."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    events = tracker.events_since(since)
    landings = tracker.landings_since(since)

    by_zone_events: dict[str, int] = {}
    by_zone_landings: dict[str, int] = {}
    coverage_exits = 0
    for event in events:
        if event.reason == "left_coverage":
            coverage_exits += 1  # خروج من نطاق المستقبلات: ليس شذوذاً
            continue
        zone = zone_at(event.last_lat, event.last_lon, event.country_code)
        if zone:
            by_zone_events[zone] = by_zone_events.get(zone, 0) + 1
    for landing in landings:
        zone = zone_at(landing.latitude, landing.longitude, landing.airport_country_code)
        if zone:
            by_zone_landings[zone] = by_zone_landings.get(zone, 0) + 1

    rows = rows if rows is not None else build_matrix(tracker, hours)
    return {
        "hours": hours,
        "events_by_zone": [
            {"zone": zone_label(key), "count": count}
            for key, count in sorted(by_zone_events.items(), key=lambda kv: -kv[1])
        ],
        "landings_by_zone": [
            {"zone": zone_label(key), "count": count}
            for key, count in sorted(by_zone_landings.items(), key=lambda kv: -kv[1])
        ],
        "flagged": len(rows),
        "coverage_exits": coverage_exits,
        "matrix": [row.as_dict() for row in rows],
        "top": [
            {
                "aircraft": row.icao24.upper(),
                "registration": row.registration,
                "score": row.score,
                "confidence": row.confidence,
                "zone": row.zone,
                "indicators": row.indicators,
            }
            for row in rows[:8]
        ],
    }
