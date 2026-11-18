"""سجل الساحل الدائم: نقاط إخفاء التعريف ومهابط الهبوط المتكرّرة (Part B).

يحوّل أحداث الانقطاع والهبوطات المرصودة إلى **سجل جغرافي دائم** في SQLite،
ثم يجمّعها شبكياً (0.25°) ليكشف «النقاط الساخنة»: أين يتكرّر انقطاع البث أو
الهبوط في مهبط غير موثّق.

مفتاح إزالة التكرار الإلزامي: ``[ICAO_Hex + Date + Entry_FIR_Timestamp]``.
هذا يمنع تضخيم التكرار عبر الدورات الساعية واليومية.

قاعدة صارمة: التكرار يعني «رصد متكرّر» لا «تأكيد هبوط». التحقق البصري يبقى
معلّقاً حتى تُراجع صورة Sentinel-2 فعلياً، ودرجة الثقة تُعلن صراحة.
"""
from __future__ import annotations

import csv
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sahel import (
    country_ar,
    dedup_key,
    sentinel_url,
    earth_url,
    zone_at,
    zone_label,
)

log = logging.getLogger("ledger")

# شبكة التجميع: 0.25° ≈ 27 كم عند خط عرض الساحل — دقّة كافية لمهبط/نقطة انقطاع
# دون أن تدمج مهابط متجاورة.
GRID_DEG = 0.25

IMINT_PENDING = "بانتظار التحقق البصري (Sentinel-2)"

HOTSPOT_FIELDS = [
    ("zone_id", "Zone_ID"),
    ("country_region", "Country_Region"),
    ("airfield", "Airfield_or_Coordinates"),
    ("frequency", "Blackout_Frequency"),
    ("hex_codes", "Associated_Hex_Codes"),
    ("imint_status", "Ground_IMINT_Status"),
    ("confidence", "Verification_Confidence"),
]


def grid_cell(lat: float | None, lon: float | None) -> tuple[float, float] | None:
    """مركز الخلية الشبكية التي تقع فيها النقطة (0.25°)."""
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return (
        round(math.floor(lat / GRID_DEG) * GRID_DEG + GRID_DEG / 2, 4),
        round(math.floor(lon / GRID_DEG) * GRID_DEG + GRID_DEG / 2, 4),
    )


def zone_id(lat: float, lon: float, zone: str | None) -> str:
    """معرّف ثابت للنقطة الساخنة، مشتق من الشبكة لا من ترتيب عشوائي."""
    prefix = {"sahel": "SAH", "transit": "TRN", "maritime": "MAR"}.get(zone or "", "OUT")
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{prefix}-{ns}{abs(lat):05.2f}-{ew}{abs(lon):06.2f}"


@dataclass
class HotspotRow:
    """صف واحد في جدول Part B (قائمة أماكن الإخفاء والهبوط)."""

    zone_id: str
    country_region: str
    airfield: str
    frequency: int
    hex_codes: list[str] = field(default_factory=list)
    imint_status: str = IMINT_PENDING
    confidence: str = "منخفضة"
    breakdown: str = ""
    lat: float | None = None
    lon: float | None = None
    zone_key: str = ""

    @property
    def sentinel_url(self) -> str:
        return sentinel_url(self.lat, self.lon)

    @property
    def earth_url(self) -> str:
        return earth_url(self.lat, self.lon)

    def as_dict(self) -> dict:
        return {
            "Zone_ID": self.zone_id,
            "Country_Region": self.country_region,
            "Airfield_or_Coordinates": self.airfield,
            "Blackout_Frequency": self.frequency,
            "Associated_Hex_Codes": "، ".join(sorted(self.hex_codes)),
            "Ground_IMINT_Status": self.imint_status,
            "Verification_Confidence": self.confidence,
            "Observation_Breakdown": self.breakdown,
            "Zone": zone_label(self.zone_key or None),
            "Sentinel_EO_Browser": self.sentinel_url,
            "Google_Earth": self.earth_url,
        }


def _confidence(frequency: int) -> str:
    if frequency >= 4:
        return "مرتفعة (نمط متكرر)"
    if frequency >= 2:
        return "متوسطة (تكرار)"
    return "منخفضة (رصد منفرد)"


# ---------------------------------------------------------------------------
def sync(tracker, hours: int = 24) -> dict:
    """يثبّت أحداث الفترة في السجل الدائم (idempotent) ويعيد عدّادات الإضافة."""
    store = getattr(tracker, "store", None)
    if store is None:
        return {"blackouts": 0, "landings": 0}

    from classifier import is_uae_related_event

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    added_blackouts = 0
    added_landings = 0

    for event in tracker.events_since(since):
        # «الخروج من نطاق التغطية» ليس انقطاعاً؛ لا يُسجَّل في سجل الإخفاء.
        if event.reason == "left_coverage":
            continue
        if not is_uae_related_event(event):
            continue  # النطاق: الطائرات الإماراتية فقط
        if event.last_lat is None or event.last_lon is None:
            continue
        cell = grid_cell(event.last_lat, event.last_lon)
        if cell is None:
            continue
        zone = zone_at(event.last_lat, event.last_lon, event.country_code)
        try:
            added = store.add_blackout_point(
                {
                    "icao24": event.icao24.lower(),
                    "registration": event.registration,
                    "observed_at": int(event.last_seen.timestamp()),
                    "lat": event.last_lat,
                    "lon": event.last_lon,
                    "country": event.country,
                    "country_code": event.country_code,
                    "zone": zone,
                    "grid_lat": cell[0],
                    "grid_lon": cell[1],
                    "reason": event.reason,
                    "dedup_key": dedup_key(event.icao24, event.last_seen, "BLK"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - السجل لا يُفشل التقرير
            log.warning("تعذّر تسجيل نقطة انقطاع %s: %s", event.icao24, exc)
            continue
        added_blackouts += int(bool(added))

    for landing in tracker.landings_since(since):
        if not landing.uae_related:
            continue
        if landing.latitude is None or landing.longitude is None:
            continue
        cell = grid_cell(landing.latitude, landing.longitude)
        if cell is None:
            continue
        zone = zone_at(
            landing.latitude, landing.longitude, landing.airport_country_code
        )
        try:
            added = store.add_landing_site(
                {
                    "icao24": landing.icao24.lower(),
                    "registration": landing.registration,
                    "observed_at": int(landing.at.timestamp()),
                    "lat": landing.latitude,
                    "lon": landing.longitude,
                    "airport_ident": landing.airport_ident,
                    "airport_name": landing.airport_name,
                    "country": None,
                    "country_code": landing.airport_country_code,
                    "zone": zone,
                    "grid_lat": cell[0],
                    "grid_lon": cell[1],
                    "dedup_key": dedup_key(landing.icao24, landing.at, "LND"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذّر تسجيل مهبط %s: %s", landing.icao24, exc)
            continue
        added_landings += int(bool(added))

    return {"blackouts": added_blackouts, "landings": added_landings}


# ---------------------------------------------------------------------------
def build_hotspots(
    tracker, hours: int = 24, min_frequency: int = 1, limit: int = 12
) -> list[HotspotRow]:
    """يجمّع السجل الدائم في نقاط ساخنة مرتّبة (الساحل أولاً ثم التكرار)."""
    store = getattr(tracker, "store", None)
    if store is None:
        return []
    since_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    try:
        blackouts = store.blackout_points_since(since_ts)
        landings = store.landing_sites_since(since_ts)
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذّر قراءة سجل الساحل: %s", exc)
        return []

    groups: dict[tuple, dict] = defaultdict(
        lambda: {
            "hex": set(), "blackouts": 0, "landings": 0, "country": None,
            "airfield": "", "zone": None, "lat": None, "lon": None,
        }
    )

    def key_of(row: dict) -> tuple:
        cell = grid_cell(row.get("lat"), row.get("lon"))
        if cell is None:
            return ()
        return (row.get("zone") or "", cell[0], cell[1])

    for row in blackouts:
        key = key_of(row)
        if not key:
            continue
        group = groups[key]
        group["hex"].add((row.get("icao24") or "").upper())
        group["blackouts"] += 1
        group["zone"] = group["zone"] or row.get("zone")
        group["country"] = group["country"] or row.get("country") or row.get("country_code")
        group["lat"] = group["lat"] if group["lat"] is not None else row.get("lat")
        group["lon"] = group["lon"] if group["lon"] is not None else row.get("lon")

    for row in landings:
        key = key_of(row)
        if not key:
            continue
        group = groups[key]
        group["hex"].add((row.get("icao24") or "").upper())
        group["landings"] += 1
        group["zone"] = group["zone"] or row.get("zone")
        group["country"] = group["country"] or row.get("country_code")
        if row.get("airport_name") or row.get("airport_ident"):
            ident = row.get("airport_ident") or ""
            name = row.get("airport_name") or ""
            group["airfield"] = f"{name} ({ident})" if name and ident else (name or ident)
        group["lat"] = group["lat"] if group["lat"] is not None else row.get("lat")
        group["lon"] = group["lon"] if group["lon"] is not None else row.get("lon")

    rows: list[HotspotRow] = []
    for (zone, cell_lat, cell_lon), group in groups.items():
        frequency = group["blackouts"] + group["landings"]
        if frequency < min_frequency:
            continue
        lat = group["lat"] if group["lat"] is not None else cell_lat
        lon = group["lon"] if group["lon"] is not None else cell_lon
        country = country_ar(group["country"]) if group["country"] else "غير محددة"
        airfield = group["airfield"] or f"{lat:.4f}, {lon:.4f} (خلية {GRID_DEG}°)"
        breakdown = f"{group['blackouts']} انقطاع · {group['landings']} هبوط"
        rows.append(
            HotspotRow(
                zone_id=zone_id(cell_lat, cell_lon, zone),
                country_region=country,
                airfield=airfield,
                frequency=frequency,
                hex_codes=sorted(h for h in group["hex"] if h),
                imint_status=IMINT_PENDING,
                confidence=_confidence(frequency),
                breakdown=breakdown,
                lat=lat,
                lon=lon,
                zone_key=zone or "",
            )
        )

    zone_rank = {"sahel": 0, "transit": 1, "maritime": 2}
    rows.sort(key=lambda row: (zone_rank.get(row.zone_key, 3), -row.frequency))
    return rows[:limit]


def export_csv(rows: list[HotspotRow], path: Path) -> Path:
    """يصدّر جدول Part B كـ CSV (بلا تكرار، بترميز UTF-8 مع BOM)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [label for _, label in HOTSPOT_FIELDS] + [
        "Observation_Breakdown", "Zone", "Sentinel_EO_Browser", "Google_Earth",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            data = row.as_dict()
            writer.writerow({key: data.get(key, "") for key in headers})
    return path
